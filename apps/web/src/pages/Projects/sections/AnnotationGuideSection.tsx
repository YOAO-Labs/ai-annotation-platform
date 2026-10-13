import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { useToastStore } from "@/components/ui/Toast";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { useUpdateProject } from "@/hooks/useProjects";
import { useUnsavedWarning } from "@/hooks/useUnsavedWarning";
import { useGuideAssets } from "@/hooks/useGuideAssets";
import { ANNOTATION_GUIDE_STARTER } from "@/components/markdown/markdownGuideStarter";
import type { GuideAssetEntry, ProjectResponse, ProjectUpdatePayload } from "@/api/projects";

const DESCRIPTION_CLASS = "m-0 text-xs leading-relaxed text-muted-foreground";
const PLACEHOLDER_CLASS =
  "rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground";
const AUTO_SAVE_DELAY_MS = 1000;

const MarkdownEditor = lazy(() =>
  import("@/components/markdown/MarkdownEditor").then((module) => ({
    default: module.MarkdownEditor,
  })),
);

type SaveStatus = "saved" | "pending" | "saving" | "failed";
type SaveMutation = (payload: ProjectUpdatePayload) => Promise<unknown>;

interface SaveRequest {
  epoch: number;
  revision: number;
  content: string;
  mutateAsync: SaveMutation;
}

interface RetainedGuideDraft {
  content: string;
  error: string | null;
  saveAttempted: boolean;
}

const retainedGuideDrafts = new Map<string, RetainedGuideDraft>();

interface GuideSaveChain {
  tail: Promise<void>;
  confirmed?: {
    content: string;
    updatedAt?: string;
  };
}

const guideSaveChains = new Map<string, GuideSaveChain>();
const guideOwnerGenerations = new Map<string, number>();

function claimGuideOwner(ownerKey: string): number {
  const generation = (guideOwnerGenerations.get(ownerKey) ?? 0) + 1;
  guideOwnerGenerations.set(ownerKey, generation);
  return generation;
}

function retainGuideDraft(
  ownerKey: string,
  content: string,
  error: string | null,
  saveAttempted: boolean,
) {
  retainedGuideDrafts.set(ownerKey, { content, error, saveAttempted });
}

function discardRetainedGuideDraft(ownerKey: string) {
  retainedGuideDrafts.delete(ownerKey);
}

function clearRetainedGuideDraft(ownerKey: string, content: string) {
  if (retainedGuideDrafts.get(ownerKey)?.content === content) {
    retainedGuideDrafts.delete(ownerKey);
  }
}

function markRetainedGuideDraftFailed(ownerKey: string, content: string, error: string) {
  const retained = retainedGuideDrafts.get(ownerKey);
  if (retained?.content === content) {
    retainedGuideDrafts.set(ownerKey, { content, error, saveAttempted: true });
  }
}

function runSerializedGuideSave(
  resourceKey: string,
  content: string,
  operation: () => Promise<unknown>,
): Promise<void> {
  const chain = guideSaveChains.get(resourceKey) ?? {
    tail: Promise.resolve(),
  };
  const confirmedOperation = async () => {
    const result = await operation();
    const updatedAt =
      typeof result === "object" &&
      result !== null &&
      "updated_at" in result &&
      typeof result.updated_at === "string"
        ? result.updated_at
        : undefined;
    chain.confirmed = { content, updatedAt };
  };
  const request = chain.tail.then(confirmedOperation, confirmedOperation);
  const tail = request.then(
    () => undefined,
    () => undefined,
  );
  chain.tail = tail;
  guideSaveChains.set(resourceKey, chain);
  void tail.then(() => {
    if (guideSaveChains.get(resourceKey) === chain && chain.tail === tail) {
      guideSaveChains.delete(resourceKey);
    }
  });
  return request;
}

export function resetRetainedGuideDraftsForTests() {
  retainedGuideDrafts.clear();
  guideSaveChains.clear();
  guideOwnerGenerations.clear();
}

function getSaveErrorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "网络或权限错误";
}

export function AnnotationGuideSection({ project }: { project: ProjectResponse }) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const ownerKey = `${userId ?? "anonymous"}:${project.id}`;
  return (
    <AnnotationGuideProjectBody
      key={ownerKey}
      ownerKey={ownerKey}
      ownerUserId={userId}
      project={project}
    />
  );
}

function AnnotationGuideProjectBody({
  project,
  ownerKey,
  ownerUserId,
}: {
  project: ProjectResponse;
  ownerKey: string;
  ownerUserId: string | null;
}) {
  const pushToast = useToastStore((state) => state.push);
  const update = useUpdateProject(project.id);
  const updateMutateAsync = update.mutateAsync;
  const { uploadAsset, deleteAsset, resolveImage } = useGuideAssets(project.id);

  const initialMarkdown =
    (project as unknown as { annotation_guide?: string | null }).annotation_guide ?? "";
  const initialAssets = useMemo(
    () => (project as unknown as { guide_assets?: GuideAssetEntry[] | null }).guide_assets ?? [],
    [project],
  );

  const retainedDraft = retainedGuideDrafts.get(ownerKey);
  const recoveredDraft = retainedDraft ?? null;
  const startingMarkdown = recoveredDraft?.content ?? initialMarkdown;
  const startingStatus: SaveStatus = recoveredDraft ? "failed" : "saved";
  const startingError = recoveredDraft
    ? (recoveredDraft.error ?? "离开页面时保存未确认，请重试")
    : null;

  const [draft, setDraft] = useState(startingMarkdown);
  const [savedMarkdown, setSavedMarkdown] = useState(initialMarkdown);
  const [assets, setAssets] = useState<GuideAssetEntry[]>(initialAssets);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>(startingStatus);
  const [saveError, setSaveError] = useState<string | null>(startingError);
  const [ownerGeneration] = useState(() => claimGuideOwner(ownerKey));

  const draftRef = useRef(startingMarkdown);
  const savedMarkdownRef = useRef(initialMarkdown);
  const revisionRef = useRef(0);
  const projectEpochRef = useRef(0);
  const projectIdRef = useRef(project.id);
  const activeSaveRef = useRef<SaveRequest | null>(null);
  const queuedSaveRef = useRef<SaveRequest | null>(null);
  const drainingRef = useRef(false);
  const drainPromiseRef = useRef<Promise<void> | null>(null);
  const autoSaveTimerRef = useRef<number | null>(null);
  const explicitSaveTimerRef = useRef<number | null>(null);
  const ownerActiveRef = useRef(false);
  const statusRef = useRef<SaveStatus>(startingStatus);
  const mustSaveRef = useRef(Boolean(recoveredDraft));

  const setStatus = useCallback((next: SaveStatus) => {
    if (!ownerActiveRef.current) return;
    statusRef.current = next;
    setSaveStatus(next);
  }, []);

  const mutateAsync = useCallback<SaveMutation>(
    (payload) => {
      if (!ownerUserId || !isCurrentAuthOwner(ownerUserId)) {
        return Promise.reject(new Error("登录账号已变更，草稿未以新账号保存"));
      }
      if (guideOwnerGenerations.get(ownerKey) !== ownerGeneration) {
        return Promise.reject(new Error("标注指引已由新页面接管，旧页面草稿未重复保存"));
      }
      return updateMutateAsync(payload);
    },
    [ownerGeneration, ownerKey, ownerUserId, updateMutateAsync],
  );

  const cancelAutoSave = useCallback(() => {
    if (autoSaveTimerRef.current !== null) {
      window.clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
  }, []);

  // The server can refresh the same project after a successful mutation. It
  // may update a clean form, but it must never clobber an active local draft.
  useEffect(() => {
    const retainedMatchesDraft = retainedGuideDrafts.get(ownerKey)?.content === draftRef.current;
    const resourceSavePending = guideSaveChains.has(project.id);

    if (initialMarkdown === draftRef.current && !retainedMatchesDraft && !resourceSavePending) {
      mustSaveRef.current = false;
      if (savedMarkdownRef.current !== initialMarkdown || statusRef.current !== "saved") {
        savedMarkdownRef.current = initialMarkdown;
        cancelAutoSave();
        setSavedMarkdown(initialMarkdown);
        setSaveError(null);
        setStatus("saved");
      }
    }

    if (projectIdRef.current !== project.id) {
      projectIdRef.current = project.id;
      projectEpochRef.current += 1;
      revisionRef.current += 1;
      queuedSaveRef.current = null;
      draftRef.current = initialMarkdown;
      savedMarkdownRef.current = initialMarkdown;
      mustSaveRef.current = false;
      setDraft(initialMarkdown);
      setSavedMarkdown(initialMarkdown);
      setAssets(initialAssets);
      setSaveError(null);
      setStatus("saved");
      return;
    }

    if (
      !mustSaveRef.current &&
      !retainedMatchesDraft &&
      !resourceSavePending &&
      draftRef.current === savedMarkdownRef.current &&
      savedMarkdownRef.current !== initialMarkdown
    ) {
      draftRef.current = initialMarkdown;
      savedMarkdownRef.current = initialMarkdown;
      setDraft(initialMarkdown);
      setSavedMarkdown(initialMarkdown);
      setSaveError(null);
      setStatus("saved");
    }
  }, [cancelAutoSave, initialAssets, initialMarkdown, ownerKey, project.id, setStatus]);

  useUnsavedWarning(draft !== savedMarkdown || saveStatus !== "saved");

  const drainSaveQueue = useCallback((): Promise<void> => {
    if (drainingRef.current) return drainPromiseRef.current ?? Promise.resolve();

    drainingRef.current = true;
    const drain = (async () => {
      try {
        while (queuedSaveRef.current) {
          const request = queuedSaveRef.current;
          queuedSaveRef.current = null;

          if (request.epoch !== projectEpochRef.current) continue;
          activeSaveRef.current = request;
          setStatus("saving");

          let failure: unknown = null;
          try {
            await runSerializedGuideSave(project.id, request.content, () =>
              request.mutateAsync({ annotation_guide: request.content }),
            );
          } catch (error: unknown) {
            failure = error;
          }
          activeSaveRef.current = null;

          if (request.epoch !== projectEpochRef.current) continue;

          if (failure === null) {
            // A response confirms the content it saved even when the user has
            // edited again. Compare against the current draft only after
            // advancing this baseline, otherwise A -> B -> A can lose its
            // queued save when B resolves.
            savedMarkdownRef.current = request.content;
            clearRetainedGuideDraft(ownerKey, request.content);
            if (ownerActiveRef.current) {
              setSavedMarkdown(request.content);
              setSaveError(null);
            }

            const currentDraft = draftRef.current;
            if (currentDraft !== request.content) {
              const queued = queuedSaveRef.current as SaveRequest | null;
              if (queued === null && autoSaveTimerRef.current === null) {
                queuedSaveRef.current = {
                  epoch: request.epoch,
                  revision: revisionRef.current,
                  content: currentDraft,
                  mutateAsync,
                };
              } else if (
                queued &&
                (queued.epoch !== request.epoch || queued.content !== currentDraft)
              ) {
                queuedSaveRef.current =
                  autoSaveTimerRef.current === null
                    ? {
                        epoch: request.epoch,
                        revision: revisionRef.current,
                        content: currentDraft,
                        mutateAsync,
                      }
                    : null;
              }
              if (ownerActiveRef.current) {
                setStatus(queuedSaveRef.current ? "saving" : "pending");
              }
            } else {
              mustSaveRef.current = false;
              cancelAutoSave();
              if (ownerActiveRef.current) setStatus("saved");
            }
            continue;
          }

          // If a newer explicit request is already waiting, let it decide the
          // final state. Otherwise retain the draft and expose a retryable
          // failure.
          if (queuedSaveRef.current) continue;
          if (draftRef.current === savedMarkdownRef.current && !mustSaveRef.current) {
            discardRetainedGuideDraft(ownerKey);
            if (ownerActiveRef.current) {
              setSaveError(null);
              setStatus("saved");
            }
            continue;
          }
          const message = getSaveErrorMessage(failure);
          markRetainedGuideDraftFailed(ownerKey, draftRef.current, message);
          if (ownerActiveRef.current) {
            setSaveError(message);
            setStatus("failed");
            pushToast({ msg: "标注指引保存失败，可重试", kind: "warning" });
          }
        }
      } finally {
        drainingRef.current = false;
        drainPromiseRef.current = null;
      }
    })();

    drainPromiseRef.current = drain;
    return drain;
  }, [cancelAutoSave, mutateAsync, ownerKey, project.id, pushToast, setStatus]);

  const enqueueSave = useCallback((): Promise<void> => {
    const content = draftRef.current;
    const epoch = projectEpochRef.current;
    const active = activeSaveRef.current;
    const queued = queuedSaveRef.current;

    if (active?.epoch === epoch && active.content === content) {
      return drainPromiseRef.current ?? Promise.resolve();
    }
    if (queued?.epoch === epoch && queued.content === content) {
      return drainPromiseRef.current ?? Promise.resolve();
    }
    if (!active && !queued && !mustSaveRef.current && content === savedMarkdownRef.current) {
      setStatus("saved");
      return Promise.resolve();
    }

    queuedSaveRef.current = {
      epoch,
      revision: revisionRef.current,
      content,
      mutateAsync,
    };
    mustSaveRef.current = true;
    retainGuideDraft(ownerKey, content, null, true);
    if (ownerActiveRef.current) setSaveError(null);
    setStatus("saving");
    return drainSaveQueue();
  }, [drainSaveQueue, mutateAsync, ownerKey, setStatus]);

  const scheduleAutoSave = useCallback(() => {
    cancelAutoSave();
    autoSaveTimerRef.current = window.setTimeout(() => {
      autoSaveTimerRef.current = null;
      void enqueueSave();
    }, AUTO_SAVE_DELAY_MS);
  }, [cancelAutoSave, enqueueSave]);

  const handleDraftChange = useCallback(
    (next: string) => {
      draftRef.current = next;
      revisionRef.current += 1;
      setDraft(next);
      setSaveError(null);

      const epoch = projectEpochRef.current;
      const queued = queuedSaveRef.current;
      if (queued && (queued.epoch !== epoch || queued.content !== next)) {
        queuedSaveRef.current = null;
      }
      const active = activeSaveRef.current;
      const queuedAfterChange = queuedSaveRef.current;
      const resourceSavePending = guideSaveChains.has(project.id);
      const previousSaveAttempted = retainedGuideDrafts.get(ownerKey)?.saveAttempted ?? false;
      const hasOutstandingSave =
        active?.epoch === epoch || queuedAfterChange?.epoch === epoch || resourceSavePending;
      const saveAttempted = previousSaveAttempted || hasOutstandingSave;
      const needsSave = next !== savedMarkdownRef.current || saveAttempted;
      mustSaveRef.current = needsSave;
      if (needsSave) {
        retainGuideDraft(ownerKey, next, null, saveAttempted);
      } else {
        discardRetainedGuideDraft(ownerKey);
      }

      if (!needsSave) {
        cancelAutoSave();
        setStatus("saved");
        return;
      }

      const requestAlreadyHasDraft =
        (active?.epoch === epoch && active.content === next) ||
        (queuedAfterChange?.epoch === epoch && queuedAfterChange.content === next);
      if (requestAlreadyHasDraft) {
        cancelAutoSave();
        setStatus("saving");
      } else {
        scheduleAutoSave();
        setStatus(hasOutstandingSave ? "saving" : "pending");
      }
    },
    [cancelAutoSave, ownerKey, project.id, scheduleAutoSave, setStatus],
  );

  const handleAutoSave = useCallback(() => {
    cancelAutoSave();
    void enqueueSave();
  }, [cancelAutoSave, enqueueSave]);

  const handleExplicitSave = useCallback(() => {
    cancelAutoSave();
    if (explicitSaveTimerRef.current !== null) {
      window.clearTimeout(explicitSaveTimerRef.current);
    }
    // A table cell is a nested Lexical editor. Its blur handler flushes the
    // cell into the parent editor before MDXEditor publishes the new Markdown.
    // Read the draft on the next task so an explicit save cannot capture the
    // pre-flush value.
    explicitSaveTimerRef.current = window.setTimeout(() => {
      explicitSaveTimerRef.current = null;
      void enqueueSave();
    }, 0);
  }, [cancelAutoSave, enqueueSave]);

  const retireFlushRef = useRef<() => void>(() => undefined);
  retireFlushRef.current = () => {
    cancelAutoSave();
    if (explicitSaveTimerRef.current !== null) {
      window.clearTimeout(explicitSaveTimerRef.current);
      explicitSaveTimerRef.current = null;
    }
    if (draftRef.current !== savedMarkdownRef.current) {
      const retainedError =
        statusRef.current === "failed" ? saveError : "离开页面时保存未确认，请重试";
      const saveAttempted =
        retainedGuideDrafts.get(ownerKey)?.saveAttempted ?? statusRef.current === "failed";
      retainGuideDraft(ownerKey, draftRef.current, retainedError, saveAttempted);
    }
    if (statusRef.current === "failed") return;
    void enqueueSave();
  };

  useEffect(() => {
    ownerActiveRef.current = true;
    return () => {
      ownerActiveRef.current = false;
      retireFlushRef.current();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const observePendingResourceSaves = async () => {
      let observedChain = guideSaveChains.get(project.id);
      let confirmed: GuideSaveChain["confirmed"];
      while (observedChain) {
        const observedTail = observedChain.tail;
        await observedTail;
        if (cancelled) return;
        if (observedChain.confirmed) {
          confirmed = observedChain.confirmed;
        }
        const nextChain = guideSaveChains.get(project.id);
        if (!nextChain || (nextChain === observedChain && nextChain.tail === observedTail)) {
          break;
        }
        observedChain = nextChain;
      }

      if (cancelled) return;
      const currentDraft = draftRef.current;
      const confirmedTime = confirmed?.updatedAt ? Date.parse(confirmed.updatedAt) : Number.NaN;
      const propsTime = Date.parse(project.updated_at);
      const versionsAreComparable = Number.isFinite(confirmedTime) && Number.isFinite(propsTime);
      const contentsDiffer = confirmed !== undefined && confirmed.content !== initialMarkdown;
      const propsVersionWins =
        contentsDiffer && versionsAreComparable && propsTime >= confirmedTime;
      const chainVersionWins = contentsDiffer && versionsAreComparable && confirmedTime > propsTime;
      const draftMatchesConfirmation = confirmed?.content === currentDraft;
      const draftWasConfirmed = draftMatchesConfirmation && !propsVersionWins;
      const untouchedConfirmedDraftWasSuperseded =
        draftMatchesConfirmation && propsVersionWins && revisionRef.current === 0;

      if (!draftWasConfirmed && !untouchedConfirmedDraftWasSuperseded) {
        if (mustSaveRef.current) return;
        if (retainedGuideDrafts.get(ownerKey)?.content === currentDraft) return;
        if (currentDraft !== savedMarkdownRef.current) return;
      } else {
        clearRetainedGuideDraft(ownerKey, currentDraft);
      }

      const confirmedMarkdown = draftWasConfirmed
        ? currentDraft
        : chainVersionWins
          ? (confirmed?.content ?? initialMarkdown)
          : initialMarkdown;
      if (confirmedMarkdown !== draftRef.current) {
        draftRef.current = confirmedMarkdown;
        setDraft(confirmedMarkdown);
      }

      mustSaveRef.current = false;
      savedMarkdownRef.current = confirmedMarkdown;
      cancelAutoSave();
      setSavedMarkdown(confirmedMarkdown);
      setSaveError(null);
      setStatus("saved");
    };

    void observePendingResourceSaves();
    return () => {
      cancelled = true;
    };
  }, [cancelAutoSave, initialMarkdown, ownerKey, project.id, project.updated_at, setStatus]);

  const handleUpload = useCallback(
    async (file: File) => {
      const epoch = projectEpochRef.current;
      const projectId = project.id;
      const result = await uploadAsset(file);
      if (epoch !== projectEpochRef.current || projectId !== projectIdRef.current) {
        throw new Error("项目已切换，上传结果已丢弃");
      }
      setAssets((previous) => [
        ...previous,
        {
          key: result.src.replace(/^guide-asset:/, ""),
          original_name: file.name,
          content_type: file.type,
          size: file.size,
          uploaded_at: new Date().toISOString(),
        },
      ]);
      return result;
    },
    [project.id, uploadAsset],
  );

  const handleDeleteAsset = useCallback(
    async (key: string) => {
      const epoch = projectEpochRef.current;
      try {
        await deleteAsset(key);
        if (epoch !== projectEpochRef.current) return;
        setAssets((previous) => previous.filter((asset) => asset.key !== key));
        pushToast({ msg: "已删除指引图片", kind: "success" });
      } catch (error: unknown) {
        pushToast({ msg: getSaveErrorMessage(error), kind: "warning" });
      }
    },
    [deleteAsset, pushToast],
  );

  const handleStarter = useCallback(() => {
    if (draftRef.current.trim()) return;
    handleDraftChange(ANNOTATION_GUIDE_STARTER);
  }, [handleDraftChange]);

  const saveButtonLabel = saveStatus === "failed" ? "重试保存" : "保存";

  return (
    <Card>
      <div className="border-b border-border px-4 py-3.5">
        <h3 className="text-sm font-semibold">标注指引</h3>
        <p className="m-0 mt-1 text-xs text-muted-foreground">
          支持可视化 Markdown、源码和 GFM 表格；拖拽或粘贴图片会上传到当前项目资源。
          标注员可通过工作台顶栏的「标注指引」按钮打开阅读窗口。 停止输入后自动保存，也可手动保存。
        </p>
      </div>
      <div className="flex flex-col gap-3 p-4">
        <Suspense fallback={<div className={PLACEHOLDER_CLASS}>编辑器加载中…</div>}>
          <MarkdownEditor
            key={project.id}
            value={draft}
            onChange={handleDraftChange}
            onUploadImage={handleUpload}
            onBlur={handleAutoSave}
            documentId={project.id}
            label="标注指引编辑器"
            variant="document"
            resolveImage={resolveImage}
            imageScope={project.id}
            placeholder="输入类别定义、易混淆边界和典型反例…"
          />
        </Suspense>

        {!draft.trim() && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-dashed border-border px-3 py-2">
            <span className="text-xs text-muted-foreground">
              从类别定义、边界规则和复核清单开始。
            </span>
            <Button size="sm" variant="ghost" onClick={handleStarter} data-testid="guide-starter">
              插入指引模板
            </Button>
          </div>
        )}

        {assets.length > 0 && (
          <>
            <p className={DESCRIPTION_CLASS}>已上传图片资源</p>
            <ul
              className="m-0 max-h-[200px] list-none overflow-auto rounded-md border border-border p-0"
              data-testid="guide-asset-list"
            >
              {assets.map((asset) => (
                <li
                  className="flex items-center gap-2 border-b border-border px-2.5 py-1.5 text-xs last:border-b-0"
                  key={asset.key}
                >
                  <span title={asset.key}>{asset.original_name}</span>
                  <span className="text-muted-foreground">{(asset.size / 1024).toFixed(1)} KB</span>
                  <button
                    type="button"
                    onClick={() => void handleDeleteAsset(asset.key)}
                    className="ml-auto cursor-pointer appearance-none rounded-sm border border-border bg-transparent px-2 py-0.5 text-xs text-status-danger"
                  >
                    删除
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2">
          <span
            className="text-xs text-muted-foreground"
            role="status"
            aria-live="polite"
            data-testid="guide-save-status"
          >
            {saveStatus === "saving"
              ? "保存中…"
              : saveStatus === "pending"
                ? "等待自动保存"
                : saveStatus === "failed"
                  ? `保存失败：${saveError ?? "请重试"}`
                  : "已保存"}
          </span>
          <Button
            size="sm"
            variant="ghost"
            onClick={handleExplicitSave}
            disabled={saveStatus === "saving"}
            data-testid="guide-save"
          >
            {saveButtonLabel}
          </Button>
        </div>
      </div>
    </Card>
  );
}
