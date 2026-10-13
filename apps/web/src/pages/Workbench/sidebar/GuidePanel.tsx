import { useEffect, useRef, useState, type RefObject } from "react";

import { GuideMarkdownView } from "@/components/markdown/GuideMarkdownView";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useGuideAssets } from "@/hooks/useGuideAssets";
import { useOnboardingProjectState } from "@/hooks/useOnboardingProjectState";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/shadcn/ui/dialog";
import { annotationGuideVersion, isGuideSeen, markGuideSeen } from "@/utils/annotationGuide";
import {
  WORKBENCH_DIALOG_CONTENT_CLASS,
  workbenchDialogOverlayClass,
} from "../shell/workbenchDialogClasses";

export interface GuidePanelProps {
  projectId: string;
  userId?: string | null;
  guideVersion?: string;
  /** 项目级 Markdown 原文; null/空字符串 → 不渲染入口. */
  content: string | null | undefined;
  /** 顶栏提供的当前项目名称，用于对话框上下文。 */
  projectName?: string;
  /** 关闭遮罩背景模糊(3D / 点云工作台,见 workbenchDialogClasses)。 */
  backdropBlur?: boolean;
}

export function guidePanelScopeKey({
  projectId,
  userId,
  guideVersion,
  content,
}: Pick<GuidePanelProps, "projectId" | "userId" | "guideVersion" | "content">): string {
  const version = guideVersion ?? annotationGuideVersion(content);
  return `${userId ?? "anonymous"}:${projectId}:${version}`;
}

/**
 * Workbench 标注指引入口与阅读对话框。
 *
 * 阅读状态只在用户点击确认后写入；关闭窗口、切换项目和版本都不会误记为已读。
 * scope key 同时作为布局层的 React key，避免异步图片和保存响应串到下一份指南。
 */
export function GuidePanel({
  projectId,
  userId,
  guideVersion,
  content,
  projectName,
  backdropBlur = true,
}: GuidePanelProps) {
  const trimmed = (content ?? "").trim();
  const version = guideVersion ?? annotationGuideVersion(content);
  const scopeKey = guidePanelScopeKey({ projectId, userId, guideVersion, content });
  const { resolveImage } = useGuideAssets(projectId);
  const {
    markGuideRead,
    retry,
    saveError,
    isSaving,
    guideRead: serverGuideRead,
  } = useOnboardingProjectState(projectId, version);
  const [open, setOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(() => isGuideSeen(userId, projectId, version));
  const scopeRef = useRef(scopeKey);
  const dialogContentRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const overlayPointerRef = useRef(false);
  const wasOpenRef = useRef(false);

  scopeRef.current = scopeKey;
  useEffect(() => {
    scopeRef.current = scopeKey;
    setOpen(false);
    setConfirmed(isGuideSeen(userId, projectId, version));
  }, [projectId, scopeKey, userId, version]);

  useEffect(() => {
    if (open) {
      wasOpenRef.current = true;
      return;
    }
    if (!wasOpenRef.current) return;
    wasOpenRef.current = false;
    triggerRef.current?.focus();
  }, [open]);

  const guideRead = serverGuideRead || confirmed || isGuideSeen(userId, projectId, version);

  const confirmRead = async () => {
    const requestScope = scopeKey;
    const ok = await markGuideRead();
    if (scopeRef.current !== requestScope || !ok) return;
    markGuideSeen(userId, projectId, version);
    setConfirmed(true);
  };

  const retryRead = async () => {
    const requestScope = scopeKey;
    const ok = await retry();
    if (scopeRef.current !== requestScope || !ok) return;
    markGuideSeen(userId, projectId, version);
    setConfirmed(true);
  };

  if (!trimmed) return null;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <div className="relative flex shrink-0 items-center">
        <DialogTrigger asChild>
          <Button
            ref={triggerRef}
            variant="ghost"
            size="sm"
            type="button"
            aria-label="标注指引"
            title="标注指引"
            data-workbench-guide-trigger=""
            data-testid="wb-guide-trigger"
            className="h-7 gap-1.5 px-2 text-muted-foreground hover:text-foreground @max-[700px]:w-7 @max-[700px]:p-0"
          >
            <Icon name="book" size={14} />
            <span className="@max-[700px]:hidden">标注指引</span>
          </Button>
        </DialogTrigger>
        {!guideRead && (
          <span
            role="status"
            aria-label="未读"
            title="有未读标注指引"
            data-testid="wb-guide-unread"
            className="pointer-events-none absolute -right-0.5 -top-0.5 size-2 rounded-full bg-status-info ring-2 ring-card"
          />
        )}
      </div>
      <DialogContent
        ref={dialogContentRef}
        showCloseButton={false}
        aria-describedby={undefined}
        data-testid="wb-guide-dialog"
        data-workbench-guide=""
        className={WORKBENCH_DIALOG_CONTENT_CLASS}
        overlayProps={{
          className: workbenchDialogOverlayClass(backdropBlur),
          "data-testid": "wb-guide-overlay",
          "data-workbench-guide": "",
          onPointerDown: (event) => {
            overlayPointerRef.current = event.target === event.currentTarget;
          },
          onClick: (event) => {
            if (event.target !== event.currentTarget || !overlayPointerRef.current) return;
            event.preventDefault();
            event.stopPropagation();
            overlayPointerRef.current = false;
            setOpen(false);
          },
        }}
        onPointerDownCapture={() => {
          overlayPointerRef.current = false;
        }}
        onPointerDownOutside={(event) => event.preventDefault()}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          dialogContentRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          triggerRef.current?.focus();
        }}
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-4 py-4 md:px-8 md:py-5">
          <div className="flex min-w-0 flex-col gap-1">
            <DialogTitle className="text-xl font-semibold text-foreground">标注指引</DialogTitle>
            <DialogDescription className="text-sm text-muted-foreground">
              {projectName ? `项目：${projectName}` : "当前项目的标注规则与示例"}
            </DialogDescription>
          </div>
          <DialogClose asChild>
            <button
              type="button"
              aria-label="关闭指引"
              title="关闭指引"
              data-testid="wb-guide-close"
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border-0 bg-transparent text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Icon name="x" size={16} />
            </button>
          </DialogClose>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-5 md:px-8 md:py-7">
          <div className="mx-auto w-full max-w-3xl">
            <GuideMarkdownView
              content={trimmed}
              resolveImage={resolveImage}
              imageScope={scopeKey}
              modalContainerRef={dialogContentRef as RefObject<HTMLElement | null>}
            />
          </div>
        </div>

        <footer
          data-testid="wb-guide-footer"
          className="flex shrink-0 flex-col gap-3 border-t border-border bg-card px-4 py-3.5 md:flex-row md:items-center md:justify-between md:px-8"
        >
          <div className="min-w-0 text-sm text-muted-foreground">
            {guideRead ? "已确认阅读当前版本" : "阅读完整指引后确认，指南更新后需重新确认"}
            {saveError && (
              <p role="alert" className="mt-1 text-xs text-status-danger">
                {saveError}
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center justify-end gap-2">
            {saveError && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => void retryRead()}
                disabled={isSaving}
                data-testid="wb-guide-retry"
              >
                {isSaving ? "保存中…" : "重试"}
              </Button>
            )}
            <Button
              type="button"
              variant="primary"
              size="sm"
              onClick={() => void confirmRead()}
              disabled={guideRead || isSaving}
              data-testid="wb-guide-confirm"
            >
              {isSaving ? "保存中…" : guideRead ? "已确认阅读" : "确认已阅读"}
            </Button>
          </div>
        </footer>
      </DialogContent>
    </Dialog>
  );
}
