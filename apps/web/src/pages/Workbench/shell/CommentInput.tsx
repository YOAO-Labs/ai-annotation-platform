import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { UserPicker, type UserPickerOption } from "@/components/UserPicker";
import { CanvasDrawingEditor } from "@/components/CanvasDrawingEditor";
import {
  commentsApi,
  type AnnotationCommentAnchor,
  type CommentAttachment,
  type CommentCanvasDrawing,
  type CommentMention,
} from "@/api/comments";
import { isCurrentAuthOwner } from "@/stores/authStore";
import { randomId } from "@/utils/id";
import {
  discussionTargetCapabilities,
  type DiscussionDraft,
  type DiscussionDraftPatch,
  type DiscussionDraftStore,
  type DiscussionSubmissionSnapshot,
} from "../state/useDiscussionDraftStore";
import { useDiscussionDraftStore } from "../state/DiscussionDraftProvider";
import {
  discussionTargetKey,
  type DiscussionOrigin,
  type DiscussionPayload,
  type DiscussionTarget,
} from "../state/discussionTypes";

// mention chip(@提及):brand 语义色 + 柔底,亮暗主题统一走 token。
// 经 raw DOM(insertMentionChip)与 React(renderCommentBody)两条路径共用,故抽成静态串。
const MENTION_CHIP = "mx-px rounded-[3px] bg-brand/15 px-1.5 py-px font-medium text-brand";

export interface CommentInputProps {
  /** Legacy annotation-only adapter. New callers should pass `target`. */
  annotationId?: string | null;
  projectId?: string | null;
  taskId?: string | null;
  /** Explicit task/annotation/Issue target for the session-backed composer. */
  target?: DiscussionTarget | null;
  /** Alias retained for callers that name the prop after the contract. */
  discussionTarget?: DiscussionTarget | null;
  /** Optional host-controlled draft; the provider store is used by default. */
  draft?: DiscussionDraft | null;
  draftStore?: DiscussionDraftStore | null;
  onDraftChange?: (draft: DiscussionDraft) => void;
  /** 项目成员候选；触发 @ 时作为 UserPicker 的源。 */
  members: UserPickerOption[];
  busy?: boolean;
  /** Reviewer 端：传入当前题图 URL，画布批注弹窗以此为背景。 */
  backgroundUrl?: string | null;
  /** v0.6.4：图像真实尺寸；画布批注按真实比例，避免 16:9 / 4:3 上批注被拉成 600×400 比例。*/
  imageWidth?: number | null;
  imageHeight?: number | null;
  /** 是否显示「画布批注」入口（仅 reviewer 端默认开启）。 */
  enableCanvasDrawing?: boolean;
  /** v0.6.4：在题图上直接画批注的桥接（与 ImageStage CanvasDrawingLayer 共享坐标系）。
   *  active=true 时，本组件不渲染入口按钮（toolbar 移到 ImageStage 上方）；
   *  result 非空表示一段绘制完成，本组件应消费并写回 canvasDrawing 后调 onConsume。*/
  liveCanvas?: {
    active: boolean;
    result: CommentCanvasDrawing | null;
    /** Result identity is optional for the old ReviewWorkbench adapter. */
    resultId?: string | null;
    origin?: DiscussionOrigin | null;
    onStart: (initial?: CommentCanvasDrawing | null, origin?: DiscussionOrigin | null) => void;
    onConsume: (resultId?: string | null) => void;
  };
  anchor?: AnnotationCommentAnchor | null;
  /** v0.11.12 · 上报当前 pending 批注，让画布把「正在编辑的评论」的批注预览出来。 */
  onPendingDrawingChange?: (drawing: CommentCanvasDrawing | null) => void;
  /** Unavailable targets stay recoverable but cannot be silently retargeted. */
  targetAvailable?: boolean;
  targetUnavailableReason?: string | null;
  onReturnToTask?: () => void;
  onSubmit: (
    payload: DiscussionPayload,
    snapshot?: DiscussionSubmissionSnapshot,
  ) => void | Promise<unknown>;
}

interface PickerState {
  open: boolean;
  /** 光标矩形（top/bottom 用于 UserPicker 判断向上/向下展开）。 */
  anchor: { left: number; top: number; bottom: number };
  /** @ 后的过滤 query。 */
  query: string;
  /** @ 起始 Range（用于替换为 chip）。 */
  triggerRange: { node: Node; offset: number } | null;
}

const MAX_ATTACH_BYTES = 20 * 1024 * 1024; // 20MB / file

function sourceLabel(source: NonNullable<AnnotationCommentAnchor["source"]>): string {
  if (source === "prediction") return "prediction";
  if (source === "interpolated") return "interpolated";
  if (source === "legacy") return "legacy bbox";
  return "manual";
}

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

/** Serialize contenteditable 内容：扁平化文本 + 抽取 mention chip 的 (offset, length, userId, displayName)。
 *  v0.6.6 起 export 给单测。 */
export function serialize(root: HTMLElement): { body: string; mentions: CommentMention[] } {
  let body = "";
  const mentions: CommentMention[] = [];

  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      body += node.textContent ?? "";
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    const uid = el.getAttribute("data-mention-uid");
    if (uid) {
      const name = el.getAttribute("data-mention-name") ?? el.textContent ?? "";
      const text = `@${name}`;
      mentions.push({
        userId: uid,
        displayName: name,
        offset: body.length,
        length: text.length,
      });
      body += text;
      return;
    }
    if (el.tagName === "BR") {
      body += "\n";
      return;
    }
    el.childNodes.forEach(walk);
    // block 元素之间补换行（避免 div 包裹时丢失换行）
    if (["DIV", "P"].includes(el.tagName) && body && !body.endsWith("\n")) {
      body += "\n";
    }
  };
  root.childNodes.forEach(walk);
  const trimmedBody = body.trim();
  const leadingTrim = body.length - body.trimStart().length;
  return {
    body: trimmedBody,
    mentions: mentions.map((mention) => ({
      ...mention,
      offset: mention.offset - leadingTrim,
    })),
  };
}

/** 把 @+name 注入到当前光标位置：插入 chip span，替换之前的 `@query` 文本。 */
function insertMentionChip(triggerRange: { node: Node; offset: number }, opt: UserPickerOption) {
  const sel = window.getSelection();
  if (!sel) return;

  // 计算 trigger（@ 字符）到当前光标之间的范围
  const r = document.createRange();
  r.setStart(triggerRange.node, triggerRange.offset);
  if (sel.rangeCount > 0) {
    const cur = sel.getRangeAt(0);
    r.setEnd(cur.endContainer, cur.endOffset);
  }
  r.deleteContents();

  const chip = document.createElement("span");
  chip.contentEditable = "false";
  chip.setAttribute("data-mention-uid", opt.id);
  chip.setAttribute("data-mention-name", opt.name);
  chip.className = MENTION_CHIP;
  chip.textContent = `@${opt.name}`;

  r.insertNode(chip);

  // 在 chip 之后追加一个空格（让用户继续输入更自然）
  const space = document.createTextNode("\u00A0");
  chip.after(space);

  // 光标放在空格文本节点内（而不是父节点子边界上）：贴着 contenteditable=false
  // chip 的边界位置会让部分中文输入法无法起组合，首个字母被直接提交为字面量。
  const newRange = document.createRange();
  newRange.setStart(space, space.length);
  newRange.collapse(true);
  sel.removeAllRanges();
  sel.addRange(newRange);
}

function blankPicker(): PickerState {
  return { open: false, anchor: { left: 0, top: 0, bottom: 0 }, query: "", triggerRange: null };
}

function cloneAnchor(
  anchor: AnnotationCommentAnchor | null | undefined,
): AnnotationCommentAnchor | null {
  return anchor ? { ...anchor } : null;
}

function cloneDrawing(
  drawing: CommentCanvasDrawing | null | undefined,
): CommentCanvasDrawing | null {
  if (!drawing) return null;
  return {
    shapes: (drawing.shapes ?? []).map((shape) => ({
      ...shape,
      points: [...shape.points],
    })),
  };
}

interface PopupCanvasSession {
  identity: string;
  targetKey: string | null;
  target: DiscussionTarget | null;
  origin: DiscussionOrigin | null;
  initial: CommentCanvasDrawing | null;
  backgroundUrl: string | null;
  imageWidth: number | null;
  imageHeight: number | null;
  anchor: AnnotationCommentAnchor | null;
}

type CanvasMode =
  | { kind: "popup"; identity: string; session: PopupCanvasSession }
  | { kind: "live"; identity: string; origin: DiscussionOrigin | null };

/** Hydrate only when a target changes or the editor DOM is newly mounted. */
function hydrateEditor(root: HTMLElement, body: string, mentions: CommentMention[]) {
  const sorted = [...mentions]
    .filter((mention) => mention.offset >= 0 && mention.length > 0)
    .sort((a, b) => a.offset - b.offset);
  root.replaceChildren();
  let cursor = 0;
  for (const mention of sorted) {
    if (mention.offset < cursor || mention.offset > body.length) continue;
    const end = mention.offset + mention.length;
    if (end > body.length) continue;
    if (mention.offset > cursor)
      root.append(document.createTextNode(body.slice(cursor, mention.offset)));
    const chip = document.createElement("span");
    chip.contentEditable = "false";
    chip.setAttribute("data-mention-uid", mention.userId);
    chip.setAttribute("data-mention-name", mention.displayName);
    chip.className = MENTION_CHIP;
    chip.textContent = `@${mention.displayName}`;
    root.append(chip);
    cursor = end;
  }
  if (cursor < body.length) root.append(document.createTextNode(body.slice(cursor)));
}

function readDraftPayload(
  editor: HTMLElement,
  fallbackAnchor: AnnotationCommentAnchor | null,
): DiscussionPayload {
  const { body, mentions } = serialize(editor);
  return {
    body,
    mentions,
    attachments: [],
    canvas_drawing: null,
    anchor: fallbackAnchor,
  };
}

export function CommentInput({
  annotationId,
  projectId,
  taskId,
  target: targetProp,
  discussionTarget,
  draft: draftProp,
  draftStore: draftStoreProp,
  onDraftChange,
  members,
  busy,
  backgroundUrl,
  imageWidth,
  imageHeight,
  enableCanvasDrawing,
  liveCanvas,
  anchor,
  onPendingDrawingChange,
  targetAvailable,
  targetUnavailableReason,
  onReturnToTask,
  onSubmit,
}: CommentInputProps) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const [picker, setPicker] = useState<PickerState>(blankPicker);
  const [attachments, setAttachments] = useState<CommentAttachment[]>([]);
  const [canvasDrawing, setCanvasDrawing] = useState<CommentCanvasDrawing | null>(null);
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [canvasSession, setCanvasSession] = useState<PopupCanvasSession | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [localTargetAvailable, setLocalTargetAvailable] = useState(true);
  const composingRef = useRef(false);
  const activeUploadRequestsRef = useRef(new Map<string, string>());
  const activeSubmissionRequestsRef = useRef(new Map<string, string>());
  const mountedRef = useRef(true);
  const [, forceRequestStateRender] = useReducer((version: number) => version + 1, 0);
  const hydratedElementRef = useRef<HTMLDivElement | null>(null);
  const hydratedTargetKeyRef = useRef<string | null>(null);
  const legacyIdentityRef = useRef<string | null>(null);
  const visibleIdentityRef = useRef<string | null>(null);
  const capturedAnchorRef = useRef<AnnotationCommentAnchor | null>(null);
  const canvasSessionRef = useRef<PopupCanvasSession | null>(null);
  const canvasOpenRef = useRef(false);
  const canvasModeRef = useRef<CanvasMode | null>(null);
  const liveResultIdRef = useRef<string | null>(null);
  const liveResultRef = useRef<CommentCanvasDrawing | null>(null);
  const pushToast = useToastStore((s) => s.push);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const bumpRequestState = useCallback(() => {
    if (mountedRef.current) forceRequestStateRender();
  }, []);
  const contextStore = useDiscussionDraftStore();
  const store = draftStoreProp ?? contextStore;
  const effectiveTarget = useMemo(
    () =>
      discussionTarget ??
      targetProp ??
      (projectId && taskId && annotationId
        ? { projectId, taskId, kind: "annotation" as const, annotationId }
        : null),
    [annotationId, discussionTarget, projectId, targetProp, taskId],
  );
  const targetKey = effectiveTarget ? discussionTargetKey(effectiveTarget) : null;
  const ownerKey = store
    ? (() => {
        const owner = store.getOwner();
        return JSON.stringify([owner.sessionId, owner.userId]);
      })()
    : "legacy";
  // The target alone is not an editor identity: a replacement auth lease can
  // render the same task/annotation while owning a different draft. Legacy
  // adapters also need the annotation fallback because targetKey can be null.
  const targetIdentity = `${ownerKey}:${targetKey ?? `legacy:${annotationId ?? "none"}`}`;
  visibleIdentityRef.current = targetIdentity;
  const storeSubscribe = useCallback(
    (listener: () => void) => store?.subscribe(listener) ?? (() => undefined),
    [store],
  );
  const storeGetDraft = useCallback(
    () => (store && effectiveTarget ? store.getDraft(effectiveTarget) : undefined),
    [effectiveTarget, store],
  );
  const storeDraft = useSyncExternalStore(storeSubscribe, storeGetDraft, storeGetDraft);
  const draft = draftProp ?? storeDraft;
  // Refs provide synchronous duplicate guards; the version state makes their
  // changes visible to the current editor without sharing state with another
  // target rendered by this component instance.
  const uploadingCurrent = activeUploadRequestsRef.current.has(targetIdentity);
  const draftSubmitting = draft?.status === "submitting" || Boolean(draft?.inFlightRequestId);
  const submittingCurrent =
    activeSubmissionRequestsRef.current.has(targetIdentity) || draftSubmitting;
  const targetCapabilities = effectiveTarget
    ? discussionTargetCapabilities(effectiveTarget)
    : { text: true, mentions: true, attachments: true, canvasDrawing: true, anchor: true };
  const attachmentsEnabled = targetCapabilities.attachments;
  const canvasDrawingEnabled = Boolean(enableCanvasDrawing && targetCapabilities.canvasDrawing);
  const sessionTarget = Boolean(store && effectiveTarget);
  const storedCanvasDraft = store && effectiveTarget ? store.getDraft(effectiveTarget) : undefined;
  const canvasDraftActive = Boolean(draft?.canvasActive || storedCanvasDraft?.canvasActive);
  const activeLiveMode = canvasModeRef.current;
  const activeLiveModeDraft =
    activeLiveMode?.kind === "live" && activeLiveMode.origin && store
      ? Boolean(store.getDraft(activeLiveMode.origin.target)?.canvasActive)
      : false;
  const effectiveAttachments = useMemo(
    () => draft?.attachments ?? (sessionTarget ? [] : attachments),
    [attachments, draft?.attachments, sessionTarget],
  );
  const effectiveCanvasDrawing = useMemo(
    () => draft?.canvas_drawing ?? (sessionTarget ? null : canvasDrawing),
    [canvasDrawing, draft?.canvas_drawing, sessionTarget],
  );
  const effectiveAnchor = useMemo(
    () =>
      draft
        ? (draft.anchor ?? anchor ?? null)
        : sessionTarget
          ? (anchor ?? null)
          : (capturedAnchorRef.current ?? anchor ?? null),
    [anchor, draft, sessionTarget],
  );
  const isAvailable = targetAvailable !== false && (draft?.targetAvailable ?? localTargetAvailable);

  useEffect(() => {
    if (store && effectiveTarget && !store.getDraft(effectiveTarget)) {
      try {
        store.ensureDraft(effectiveTarget);
      } catch {
        // The host auth lease may have changed between render and this
        // effect; the replacement provider owns the new draft scope.
      }
    }
  }, [effectiveTarget, store, targetKey]);

  useEffect(() => {
    if (!store || !effectiveTarget || targetAvailable === undefined) return;
    const current = store.getDraft(effectiveTarget);
    if (current?.targetAvailable !== targetAvailable) {
      try {
        store.setTargetAvailability(effectiveTarget, targetAvailable, targetUnavailableReason);
      } catch {
        // See the owner-lease guard above.
      }
    }
  }, [effectiveTarget, store, targetAvailable, targetUnavailableReason, targetKey]);

  // Hydration is keyed by owner + target identity. Draft revisions update the
  // store but never rewrite this DOM node, so typing cannot move the caret.
  useLayoutEffect(() => {
    const editor = editorRef.current;
    if (!editor || hydratedElementRef.current !== editor) {
      if (!editor) return;
      hydratedElementRef.current = editor;
      hydratedTargetKeyRef.current = null;
    }
    if (hydratedTargetKeyRef.current === targetIdentity) return;
    hydrateEditor(editor, draft?.body ?? "", draft?.mentions ?? []);
    hydratedTargetKeyRef.current = targetIdentity;
    setPicker(blankPicker());
  }, [draft, targetIdentity]);

  useEffect(() => {
    const identity = targetIdentity;
    if (legacyIdentityRef.current === null) {
      legacyIdentityRef.current = identity;
      return;
    }
    if (legacyIdentityRef.current === identity) return;
    legacyIdentityRef.current = identity;
    capturedAnchorRef.current = null;
    setAttachments([]);
    setCanvasDrawing(null);
    // Close a modal opened for the previous target. Keep its frozen identity
    // and origin so a queued onSave cannot fall back to the newly visible one.
    if (canvasModeRef.current?.kind === "popup") {
      canvasModeRef.current = null;
      bumpRequestState();
    }
    const liveMode = canvasModeRef.current;
    if (liveMode?.kind === "live" && liveMode.identity !== identity && !liveCanvas?.active) {
      canvasModeRef.current = null;
      bumpRequestState();
    }
    canvasOpenRef.current = false;
    setCanvasOpen(false);
    setLocalError(null);
    setLocalTargetAvailable(true);
    setPicker(blankPicker());
  }, [
    annotationId,
    bumpRequestState,
    liveCanvas?.active,
    store,
    activeLiveModeDraft,
    targetIdentity,
  ]);

  const patchDraft = useCallback(
    (patch: DiscussionDraftPatch): boolean => {
      if (store && effectiveTarget) {
        try {
          store.patchDraft(effectiveTarget, patch);
          return true;
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          setLocalError(message);
          pushToast({ msg: "此发送目标不支持该内容", sub: message, kind: "warning" });
          return false;
        }
      }
      if (draftProp && onDraftChange) {
        const next = {
          ...draftProp,
          ...(patch.body === undefined && patch.text === undefined
            ? {}
            : {
                body: patch.body ?? patch.text ?? draftProp.body,
                text: patch.body ?? patch.text ?? draftProp.text,
              }),
          ...(patch.mentions === undefined ? {} : { mentions: patch.mentions }),
          ...(patch.attachments === undefined ? {} : { attachments: patch.attachments }),
          ...(patch.canvas_drawing === undefined ? {} : { canvas_drawing: patch.canvas_drawing }),
          ...(patch.anchor === undefined ? {} : { anchor: patch.anchor }),
        } as DiscussionDraft;
        onDraftChange(next);
        return true;
      }
      if (patch.attachments !== undefined) setAttachments(patch.attachments);
      if (patch.canvas_drawing !== undefined) setCanvasDrawing(patch.canvas_drawing);
      return true;
    },
    [draftProp, effectiveTarget, onDraftChange, pushToast, store],
  );

  const maybeCaptureAnchor = useCallback(() => {
    if (!anchor) return;
    if (store && effectiveTarget) {
      const current = store.getDraft(effectiveTarget);
      if (!current?.anchor) store.captureAnchor(effectiveTarget, anchor);
      return;
    }
    if (!capturedAnchorRef.current) capturedAnchorRef.current = cloneAnchor(anchor);
  }, [anchor, effectiveTarget, store]);

  const captureDrawingAnchor = useCallback(
    (target: DiscussionTarget | null, frozenAnchor: AnnotationCommentAnchor | null) => {
      if (!frozenAnchor) return;
      if (store && target) {
        const current = store.getDraft(target);
        if (!current?.anchor) store.captureAnchor(target, frozenAnchor);
        return;
      }
      if (!capturedAnchorRef.current) capturedAnchorRef.current = cloneAnchor(frozenAnchor);
    },
    [store],
  );

  const syncEditorDraft = useCallback(() => {
    const editor = editorRef.current;
    if (!editor) return false;
    const payload = readDraftPayload(editor, effectiveAnchor);
    maybeCaptureAnchor();
    return patchDraft({ body: payload.body, mentions: payload.mentions });
  }, [effectiveAnchor, maybeCaptureAnchor, patchDraft]);

  /** 同步草稿并维护光标处的 @ query（非组合期调用）。 */
  const processEditorInput = useCallback(() => {
    if (syncEditorDraft() === false) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    const offset = range.startOffset;
    if (node.nodeType !== Node.TEXT_NODE || !targetCapabilities.mentions) {
      setPicker((p) => (p.open ? blankPicker() : p));
      return;
    }
    const text = node.textContent ?? "";
    let at = -1;
    for (let i = offset - 1; i >= 0; i--) {
      const ch = text[i];
      if (ch === "@") {
        if (i === 0 || /[\s\u00A0]/.test(text[i - 1])) at = i;
        break;
      }
      if (/[\s\u00A0]/.test(ch)) break;
    }
    if (at < 0) {
      setPicker((p) => (p.open ? blankPicker() : p));
      return;
    }
    const query = text.slice(at + 1, offset);
    const tmpRange = document.createRange();
    tmpRange.setStart(node, at);
    tmpRange.setEnd(node, offset);
    const rect = tmpRange.getBoundingClientRect();
    setPicker({
      open: true,
      anchor: { left: rect.left, top: rect.top, bottom: rect.bottom },
      query,
      triggerRange: { node, offset: at },
    });
  }, [syncEditorDraft, targetCapabilities.mentions]);

  /** 监听 input：中文输入法组合期不做同步/状态更新，避免打断组合。 */
  const handleInput = useCallback(() => {
    if (composingRef.current) return;
    processEditorInput();
  }, [processEditorInput]);

  const handleCompositionEnd = useCallback(() => {
    composingRef.current = false;
    // Chrome 通常会在 compositionend 后再补一次 input；这里显式同步，保证
    // 浏览器不补事件时草稿与 @ query 也拿到最终文本。
    processEditorInput();
  }, [processEditorInput]);

  const handlePick = useCallback(
    (opt: UserPickerOption) => {
      if (!picker.triggerRange) return;
      insertMentionChip(picker.triggerRange, opt);
      setPicker(blankPicker());
      editorRef.current?.focus();
      syncEditorDraft();
    },
    [picker.triggerRange, syncEditorDraft],
  );

  const handleCanvasDraftChange = useCallback(
    (drawing: CommentCanvasDrawing | null) => {
      const session = canvasSession;
      if (!session || canvasSessionRef.current !== session) return;
      // The callback is bound to the popup's opening identity. A late callback
      // after task/annotation/account replacement may never retarget itself.
      if (visibleIdentityRef.current !== session.identity) return;
      const normalized = cloneDrawing(drawing);
      if (session.origin && store) {
        if (!store.isOwned(session.origin)) return;
        if (!store.saveDrawing(session.origin, normalized, { active: false })) return;
      } else {
        setCanvasDrawing(normalized);
      }
      if (normalized?.shapes?.length) captureDrawingAnchor(session.target, session.anchor);
    },
    [canvasSession, captureDrawingAnchor, store],
  );

  const handleCanvasSave = useCallback(
    (drawing: CommentCanvasDrawing | null) => {
      const session = canvasSession;
      if (!session || canvasSessionRef.current !== session) return;
      // Check the live identity as well as the callback's captured identity:
      // React can deliver an already queued callback after a target switch.
      if (visibleIdentityRef.current !== session.identity) return;
      const normalized = cloneDrawing(drawing);
      if (session.origin && store) {
        if (
          !store.isOwned(session.origin) ||
          discussionTargetKey(session.origin.target) !== session.targetKey ||
          !store.acceptDrawing(session.origin, normalized)
        )
          return;
      } else {
        setCanvasDrawing(normalized);
      }
      if (normalized?.shapes?.length) captureDrawingAnchor(session.target, session.anchor);
      canvasOpenRef.current = false;
      if (canvasModeRef.current?.kind === "popup" && canvasModeRef.current.session === session) {
        canvasModeRef.current = null;
      }
      setCanvasOpen(false);
    },
    [canvasSession, captureDrawingAnchor, store],
  );

  const handleCanvasClose = useCallback(() => {
    const session = canvasSession;
    if (!session || canvasSessionRef.current !== session) return;
    if (visibleIdentityRef.current !== session.identity) return;
    canvasOpenRef.current = false;
    if (canvasModeRef.current?.kind === "popup" && canvasModeRef.current.session === session) {
      canvasModeRef.current = null;
    }
    setCanvasOpen(false);
  }, [canvasSession]);

  // v0.6.4：消费来自 ImageStage 的 live canvas 结果. A result with an
  // origin is never attached to the currently visible target by inference.
  useEffect(() => {
    const result = liveCanvas?.result;
    if (!result) {
      // A cancelled live session has no result to consume. Release the local
      // duplicate-click guard once the owning stage reports it inactive.
      const mode = canvasModeRef.current;
      const modeDraftActive = activeLiveModeDraft;
      if (
        mode?.kind === "live" &&
        !liveCanvas?.active &&
        (mode.identity !== targetIdentity || !modeDraftActive)
      ) {
        canvasModeRef.current = null;
        bumpRequestState();
      }
      return;
    }
    const resultId = liveCanvas.resultId ?? null;
    if (resultId && liveResultIdRef.current === resultId) return;
    if (!resultId && liveResultRef.current === result) return;
    if (resultId) liveResultIdRef.current = resultId;
    liveResultRef.current = result;
    const origin = liveCanvas.origin;
    if (store && effectiveTarget) {
      if (origin && store.isOwned(origin) && store.acceptDrawing(origin, result)) {
        // A late result may update an inactive target's memory draft, but it
        // must never capture the currently playing frame into that target.
        if (
          origin.target.kind === "annotation" &&
          effectiveTarget &&
          discussionTargetKey(origin.target) === discussionTargetKey(effectiveTarget)
        ) {
          maybeCaptureAnchor();
        }
      }
      // Consume is always keyed. The root bridge decides whether a stale
      // result ID can be discarded; it cannot clear a newer result.
      liveCanvas.onConsume(resultId);
    } else {
      // An origin-bearing result requires a session store to validate its
      // owner/target. The legacy adapter has no origin and remains compatible.
      if (!origin) setCanvasDrawing(result.shapes?.length ? result : null);
      liveCanvas.onConsume(resultId);
    }
    const mode = canvasModeRef.current;
    if (mode?.kind === "live" && (mode.origin?.requestId ?? null) === (origin?.requestId ?? null)) {
      canvasModeRef.current = null;
      bumpRequestState();
    }
  }, [
    bumpRequestState,
    activeLiveModeDraft,
    canvasDraftActive,
    effectiveTarget,
    liveCanvas,
    maybeCaptureAnchor,
    store,
    targetIdentity,
  ]);

  // v0.11.12：把当前 pending 批注上报给画布预览通道；卸载时清空。
  useEffect(() => {
    onPendingDrawingChange?.(effectiveCanvasDrawing);
    return () => onPendingDrawingChange?.(null);
  }, [effectiveCanvasDrawing, onPendingDrawingChange]);

  const reset = useCallback(() => {
    editorRef.current?.replaceChildren();
    setAttachments([]);
    setCanvasDrawing(null);
    setPicker(blankPicker());
    setLocalError(null);
    capturedAnchorRef.current = null;
  }, []);

  const handleFileUpload = useCallback(
    async (files: FileList | null) => {
      if (
        !files ||
        files.length === 0 ||
        activeUploadRequestsRef.current.has(targetIdentity) ||
        busy ||
        !attachmentsEnabled
      )
        return;
      const uploadTarget = effectiveTarget;
      const uploadAnnotationId =
        uploadTarget?.kind === "annotation" ? uploadTarget.annotationId : annotationId;
      if (!uploadAnnotationId) return;
      const uploadIdentity = targetIdentity;
      const uploadRequestId = `upload-${randomId()}`;
      const uploadOrigin: DiscussionOrigin | null =
        store && uploadTarget
          ? {
              owner: store.getOwner(),
              target: { ...uploadTarget },
              requestId: uploadRequestId,
            }
          : null;
      activeUploadRequestsRef.current.set(uploadIdentity, uploadRequestId);
      bumpRequestState();
      try {
        for (const f of Array.from(files)) {
          const uploadStillCurrent = () =>
            activeUploadRequestsRef.current.get(uploadIdentity) === uploadRequestId &&
            (store && uploadOrigin
              ? store.isOwned(uploadOrigin)
              : visibleIdentityRef.current === uploadIdentity &&
                legacyIdentityRef.current === uploadIdentity);
          // Do not start another init after logout/account or target change.
          if (!uploadStillCurrent()) return;
          if (f.size > MAX_ATTACH_BYTES) {
            pushToast({ msg: `${f.name} 超过 20MB，已跳过`, kind: "warning" });
            continue;
          }
          const init = await commentsApi.attachmentUploadInit(uploadAnnotationId, {
            file_name: f.name,
            content_type: f.type || "application/octet-stream",
          });
          // The init response is tied to the owner and annotation captured
          // above. A lease change must stop before using its signed URL.
          if (!uploadStillCurrent()) return;
          const putRes = await fetch(init.upload_url, {
            method: "PUT",
            body: f,
            headers: { "Content-Type": f.type || "application/octet-stream" },
          });
          if (!putRes.ok) throw new Error(`上传失败 (HTTP ${putRes.status})`);
          if (!uploadStillCurrent()) return;
          const attachment: CommentAttachment = {
            storageKey: init.storage_key,
            fileName: f.name,
            mimeType: f.type || "application/octet-stream",
            size: f.size,
          };
          if (store && uploadOrigin) {
            store.acceptUpload(uploadOrigin, attachment);
          } else if (uploadStillCurrent()) {
            setAttachments((prev) => [...prev, attachment]);
          }
        }
      } catch (err) {
        pushToast({ msg: "附件上传失败", sub: String(err), kind: "error" });
      } finally {
        if (activeUploadRequestsRef.current.get(uploadIdentity) === uploadRequestId) {
          activeUploadRequestsRef.current.delete(uploadIdentity);
          bumpRequestState();
        }
      }
    },
    [
      annotationId,
      busy,
      effectiveTarget,
      attachmentsEnabled,
      bumpRequestState,
      pushToast,
      store,
      targetIdentity,
    ],
  );

  const authOwnerIsUsable = useCallback(() => {
    if (!store || draftStoreProp) return true;
    try {
      // A provider-backed store represents a real authenticated route. Fail
      // closed when auth is missing/expired; explicit draftStore adapters are
      // the supported path for standalone tests and ReviewWorkbench callers.
      return isCurrentAuthOwner(store.getOwner().userId);
    } catch {
      return false;
    }
  }, [draftStoreProp, store]);

  const handleSubmit = useCallback(async () => {
    if (
      !editorRef.current ||
      busy ||
      activeUploadRequestsRef.current.has(targetIdentity) ||
      activeSubmissionRequestsRef.current.has(targetIdentity) ||
      draftSubmitting ||
      !isAvailable ||
      (liveCanvas?.active ?? false) ||
      canvasOpenRef.current ||
      Boolean(canvasModeRef.current) ||
      canvasDraftActive
    ) {
      return;
    }
    const { body, mentions } = serialize(editorRef.current);
    const payload: DiscussionPayload = {
      body,
      mentions,
      attachments: [...effectiveAttachments],
      canvas_drawing: effectiveCanvasDrawing,
      anchor: effectiveAnchor,
    };
    if (
      !body.trim() &&
      payload.attachments.length === 0 &&
      (payload.canvas_drawing?.shapes?.length ?? 0) === 0
    )
      return;
    if (!authOwnerIsUsable()) {
      setLocalError("登录状态已变化，请重新打开评论后再试");
      return;
    }
    let submission: DiscussionSubmissionSnapshot | null = null;
    const submittedEditor = editorRef.current;
    const submittedIdentity = targetIdentity;
    let requestId: string | null = null;
    try {
      if (store && effectiveTarget) {
        // Ensure the final editor state is represented in the structured draft
        // before taking the immutable request snapshot.
        if (!patchDraft({ body, mentions })) return;
        submission = store.beginSubmission(effectiveTarget, payload);
        if (!submission) return;
      }
      requestId = submission?.requestId ?? `comment-${randomId()}`;
      // Store-backed submissions are already serialized by draft.inFlight;
      // this map also covers legacy adapters and closes the rapid-click gap
      // before React can publish the next render.
      if (activeSubmissionRequestsRef.current.has(submittedIdentity)) return;
      activeSubmissionRequestsRef.current.set(submittedIdentity, requestId);
      bumpRequestState();
      await onSubmit(payload, submission ?? undefined);
      if (store && submission) {
        // Only clear the editor when the store confirms the snapshot was
        // unchanged. A newer edit must remain visible and recoverable.
        const cleared = store.resolveSubmission(submission);
        if (
          cleared &&
          editorRef.current === submittedEditor &&
          visibleIdentityRef.current === submittedIdentity
        ) {
          reset();
        }
      } else if (
        editorRef.current === submittedEditor &&
        visibleIdentityRef.current === submittedIdentity
      ) {
        reset();
      }
      if (visibleIdentityRef.current === submittedIdentity) setLocalError(null);
    } catch (error) {
      if (store && submission) store.rejectSubmission(submission, error);
      else if (visibleIdentityRef.current === submittedIdentity)
        setLocalError(error instanceof Error ? error.message : String(error));
    } finally {
      if (requestId && activeSubmissionRequestsRef.current.get(submittedIdentity) === requestId) {
        activeSubmissionRequestsRef.current.delete(submittedIdentity);
        bumpRequestState();
      }
    }
  }, [
    authOwnerIsUsable,
    busy,
    canvasDraftActive,
    effectiveAnchor,
    effectiveAttachments,
    effectiveCanvasDrawing,
    effectiveTarget,
    bumpRequestState,
    draftSubmitting,
    isAvailable,
    liveCanvas?.active,
    onSubmit,
    patchDraft,
    reset,
    store,
    targetIdentity,
  ]);

  const startLiveCanvas = useCallback(() => {
    if (
      !liveCanvas ||
      liveCanvas.active ||
      canvasOpenRef.current ||
      canvasModeRef.current ||
      canvasDraftActive ||
      !isAvailable ||
      busy ||
      uploadingCurrent ||
      submittingCurrent ||
      !canvasDrawingEnabled
    )
      return;
    const origin = store && effectiveTarget ? store.makeOrigin(effectiveTarget) : null;
    if (origin && store) {
      if (!store.startCanvasSession(origin, effectiveCanvasDrawing)) return;
    }
    // Set this before invoking the host callback. The host may update its
    // active prop asynchronously, and two same-tick clicks must still share
    // one canvas owner.
    canvasModeRef.current = { kind: "live", identity: targetIdentity, origin };
    bumpRequestState();
    liveCanvas.onStart(effectiveCanvasDrawing, origin);
  }, [
    busy,
    bumpRequestState,
    canvasDraftActive,
    effectiveCanvasDrawing,
    effectiveTarget,
    isAvailable,
    canvasDrawingEnabled,
    liveCanvas,
    submittingCurrent,
    store,
    targetIdentity,
    uploadingCurrent,
  ]);

  const openCanvasEditor = useCallback(() => {
    if (
      canvasOpenRef.current ||
      canvasModeRef.current ||
      liveCanvas?.active ||
      canvasDraftActive ||
      !isAvailable ||
      !backgroundUrl ||
      busy ||
      uploadingCurrent ||
      submittingCurrent ||
      !canvasDrawingEnabled
    )
      return;
    const origin = store && effectiveTarget ? store.makeOrigin(effectiveTarget) : null;
    if (store && effectiveTarget && !origin) return;
    const session: PopupCanvasSession = {
      identity: targetIdentity,
      targetKey,
      target: effectiveTarget ? { ...effectiveTarget } : null,
      origin,
      initial: cloneDrawing(effectiveCanvasDrawing),
      backgroundUrl,
      imageWidth: imageWidth ?? null,
      imageHeight: imageHeight ?? null,
      anchor: cloneAnchor(effectiveAnchor),
    };
    canvasSessionRef.current = session;
    canvasModeRef.current = { kind: "popup", identity: targetIdentity, session };
    canvasOpenRef.current = true;
    setCanvasSession(session);
    setCanvasOpen(true);
  }, [
    backgroundUrl,
    busy,
    canvasDraftActive,
    effectiveAnchor,
    effectiveCanvasDrawing,
    effectiveTarget,
    imageHeight,
    imageWidth,
    isAvailable,
    canvasDrawingEnabled,
    liveCanvas?.active,
    submittingCurrent,
    store,
    targetIdentity,
    targetKey,
    uploadingCurrent,
  ]);

  const submitDisabled =
    busy ||
    uploadingCurrent ||
    submittingCurrent ||
    !isAvailable ||
    (liveCanvas?.active ?? false) ||
    canvasOpenRef.current ||
    Boolean(canvasModeRef.current) ||
    canvasDraftActive;
  const displayError = draft?.error ?? localError;

  return (
    <div className="flex flex-col gap-1.5" data-workbench-discussion>
      {!isAvailable && (
        <div className="flex items-center justify-between gap-2 rounded border border-status-danger/40 bg-status-danger-soft px-2 py-1.5 text-xs text-status-danger">
          <span>
            {draft?.unavailableReason ?? targetUnavailableReason ?? "此讨论目标已不可访问"}
          </span>
          {onReturnToTask && (
            <button
              type="button"
              className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-xs text-brand underline"
              onClick={onReturnToTask}
            >
              返回任务留言
            </button>
          )}
        </div>
      )}
      <div
        ref={editorRef}
        contentEditable={!busy && isAvailable}
        suppressContentEditableWarning
        role="textbox"
        aria-label="留言"
        aria-multiline="true"
        onInput={handleInput}
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={handleCompositionEnd}
        onKeyDown={(e) => {
          // Enter 提交（Shift+Enter 换行）
          if (
            e.key === "Enter" &&
            !e.shiftKey &&
            !picker.open &&
            !composingRef.current &&
            !e.nativeEvent.isComposing &&
            e.nativeEvent.keyCode !== 229
          ) {
            e.preventDefault();
            void handleSubmit();
          }
        }}
        data-placeholder={
          targetCapabilities.mentions
            ? effectiveTarget?.kind === "task"
              ? "留言（@ 提及成员）..."
              : "留言（@ 提及成员，可附图）..."
            : effectiveTarget?.kind === "issue"
              ? "输入问题回复…"
              : "输入任务留言…"
        }
        className="max-h-40 min-h-[56px] overflow-y-auto whitespace-pre-wrap rounded border border-border bg-card px-2 py-1.5 text-xs text-foreground outline-none [font:inherit] empty:before:text-muted-foreground empty:before:content-[attr(data-placeholder)]"
      />
      {effectiveAnchor?.kind === "video_frame" && (
        <div
          data-testid="comment-anchor-preview"
          className="inline-flex select-none items-center gap-1.5 self-start rounded border border-border bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
        >
          <Icon name="film" size={12} />
          <span className="mono">F{effectiveAnchor.frameIndex}</span>
          {effectiveAnchor.trackId && (
            <span className="mono">{effectiveAnchor.trackId.slice(0, 8)}</span>
          )}
          {effectiveAnchor.source && <span>{sourceLabel(effectiveAnchor.source)}</span>}
        </div>
      )}
      {effectiveAttachments.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {effectiveAttachments.map((a, i) => (
            <div
              key={a.storageKey}
              className="inline-flex items-center gap-1 rounded-[3px] border border-border bg-muted px-1.5 py-0.5 text-xs text-foreground"
              title={`${(a.size / 1024).toFixed(1)} KB`}
            >
              <Icon name="folder" size={11} />
              <span className="max-w-[140px] overflow-hidden text-ellipsis whitespace-nowrap">
                {a.fileName}
              </span>
              <button
                type="button"
                onClick={() =>
                  patchDraft({ attachments: effectiveAttachments.filter((_, j) => j !== i) })
                }
                className="inline-flex cursor-pointer appearance-none items-center border-0 bg-transparent p-0 text-muted-foreground"
                aria-label="移除附件"
              >
                <Icon name="x" size={10} />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="flex items-center justify-between gap-1.5">
        <div className="flex items-center gap-2">
          {attachmentsEnabled && (
            <label
              className={cn(
                "inline-flex items-center gap-1 text-xs text-muted-foreground",
                uploadingCurrent ? "cursor-wait" : "cursor-pointer",
              )}
            >
              <Icon name="upload" size={12} />
              {uploadingCurrent ? "上传中…" : "附件"}
              <input
                type="file"
                multiple
                disabled={uploadingCurrent || busy || !isAvailable}
                onChange={(e) => {
                  void handleFileUpload(e.target.files);
                  e.currentTarget.value = "";
                }}
                className="hidden"
              />
            </label>
          )}
          {canvasDrawingEnabled && (
            <button
              type="button"
              onClick={openCanvasEditor}
              disabled={
                !backgroundUrl ||
                !isAvailable ||
                submittingCurrent ||
                busy ||
                uploadingCurrent ||
                canvasOpenRef.current ||
                Boolean(liveCanvas?.active) ||
                canvasDraftActive ||
                Boolean(canvasModeRef.current)
              }
              className={cn(
                "inline-flex cursor-pointer appearance-none items-center gap-1 border-0 bg-transparent p-0 text-xs font-normal text-muted-foreground",
                effectiveCanvasDrawing && "font-semibold text-brand",
                !backgroundUrl && "cursor-default text-muted-foreground/60",
              )}
              title={
                backgroundUrl
                  ? "在弹窗中圈点说明，随评论发送，不修改标注"
                  : "题图未加载，无法在空白画布上批注"
              }
            >
              <Icon name="edit" size={12} />
              {effectiveCanvasDrawing
                ? `批注 · ${(effectiveCanvasDrawing.shapes ?? []).length} 条`
                : "弹窗批注"}
            </button>
          )}
          {liveCanvas && canvasDrawingEnabled && (
            <button
              type="button"
              onClick={startLiveCanvas}
              disabled={
                liveCanvas.active ||
                !isAvailable ||
                submittingCurrent ||
                busy ||
                uploadingCurrent ||
                canvasOpenRef.current ||
                canvasDraftActive ||
                Boolean(canvasModeRef.current)
              }
              className={cn(
                "inline-flex cursor-pointer appearance-none items-center gap-1 border-0 bg-transparent p-0 text-xs font-normal text-brand",
                liveCanvas.active && "cursor-default text-muted-foreground/60",
              )}
              title="在题图上圈点说明，随评论发送，不修改标注；缩放和平移自动跟随"
            >
              <Icon name="target" size={12} />
              {liveCanvas.active ? "正在绘制…" : "在题图上绘制"}
            </button>
          )}
        </div>
        <Button size="sm" variant="primary" disabled={submitDisabled} onClick={handleSubmit}>
          {busy || submittingCurrent ? "发送中..." : "发送"}
        </Button>
      </div>
      {displayError && (
        <div
          className="flex items-center justify-between gap-2 text-xs text-status-danger"
          role="alert"
        >
          <span>{displayError}</span>
          {draft?.status === "error" && (
            <button
              type="button"
              className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-xs text-brand underline"
              onClick={() => void handleSubmit()}
              disabled={submitDisabled}
            >
              重试
            </button>
          )}
        </div>
      )}
      {canvasDrawingEnabled && (
        <CanvasDrawingEditor
          key={targetIdentity}
          open={canvasOpen && canvasSession?.identity === targetIdentity}
          onClose={handleCanvasClose}
          onSave={handleCanvasSave}
          onDraftChange={handleCanvasDraftChange}
          initial={canvasSession?.initial}
          backgroundUrl={canvasSession?.backgroundUrl}
          imageWidth={canvasSession?.imageWidth}
          imageHeight={canvasSession?.imageHeight}
        />
      )}
      {picker.open && (
        <UserPicker
          anchor={picker.anchor}
          options={members}
          query={picker.query}
          onPick={handlePick}
          onClose={() => setPicker((p) => ({ ...p, open: false }))}
        />
      )}
    </div>
  );
}

/** 把后端返回的 body + mentions[] 还原成 React 节点（用于历史评论渲染）。
 *  渲染规则：mentions 按 offset 排序，依次插入 chip；其它文字作为纯文本。 */
export function renderCommentBody(
  body: string,
  mentions: CommentMention[],
  onMentionClick?: (userId: string) => void,
) {
  if (mentions.length === 0) return body;
  const sorted = [...mentions].sort((a, b) => a.offset - b.offset);
  const parts: ReactNode[] = [];
  let cursor = 0;
  sorted.forEach((m, i) => {
    if (m.offset > cursor) parts.push(body.slice(cursor, m.offset));
    parts.push(
      <span
        key={i}
        onClick={() => onMentionClick?.(m.userId)}
        className={cn(MENTION_CHIP, onMentionClick ? "cursor-pointer" : "cursor-default")}
      >
        @{m.displayName}
      </span>,
    );
    cursor = m.offset + m.length;
  });
  if (cursor < body.length) parts.push(body.slice(cursor));
  return parts;
}
