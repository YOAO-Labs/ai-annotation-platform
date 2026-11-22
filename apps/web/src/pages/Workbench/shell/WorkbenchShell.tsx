import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import { useFabRevealed } from "@/stores/fabRevealStore";
import { VideoMaskCorrectionDialog } from "../stage/VideoMaskCorrectionDialog";
import { MaskConversionDialog } from "../stage/MaskConversionDialog";
import { VideoTrackerReviewBar } from "../stage/VideoTrackerReviewBar";
import { useWorkbenchShellModel } from "../state/useWorkbenchShellModel";
import { IssueCreateModal } from "./IssueCreateModal";
import { WorkbenchLayout } from "./WorkbenchLayout";
import { WorkbenchSkeleton } from "./WorkbenchSkeleton";

const ISSUE_FAB_CLASS =
  "fixed right-6 z-workbench-top inline-flex size-10 cursor-pointer appearance-none items-center justify-center rounded-full border border-border bg-card text-foreground shadow-lg hover:bg-muted transition-all duration-300 ease-out";
const FAB_HIDDEN_CLASS = "translate-x-[150%] opacity-0 pointer-events-none";

export function WorkbenchShell({ mode = "annotate" }: { mode?: "annotate" | "review" }) {
  const model = useWorkbenchShellModel({ mode });
  const fabRevealed = useFabRevealed();

  if (model.kind === "loading") {
    return <WorkbenchSkeleton />;
  }

  if (model.kind === "empty") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
        <Icon name={model.emptyState.icon} size={40} />
        <div className="text-md">{model.emptyState.message}</div>
        <Button onClick={model.emptyState.onBack}>
          <Icon name="chevLeft" size={12} />
          返回
        </Button>
      </div>
    );
  }

  return (
    <>
      <WorkbenchLayout
        {...model.layout}
        videoTracker={model.propagateDialog}
        stageOverlay={
          <>
            <VideoMaskCorrectionDialog {...model.maskCorrectionDialog} />
            <MaskConversionDialog {...model.conversionDialog} />
            <VideoTrackerReviewBar {...model.trackerReview} />
            {model.issueSection &&
              model.issueSection.stageKind !== "3d" &&
              !model.issueSection.issuePinsComplete && (
                <div
                  className="absolute bottom-8 right-4 z-workbench-top flex max-w-sm items-center gap-2 rounded border border-border bg-card px-2 py-1.5 text-xs text-muted-foreground shadow-lg"
                  role="status"
                  data-testid="issue-pin-coverage"
                  data-status={model.issueSection.issuePinsError ? "error" : "loading"}
                >
                  <span>
                    {model.issueSection.issuePinsError
                      ? "部分图钉尚未加载，已显示的图钉仍可使用"
                      : `图钉加载中，已显示 ${model.issueSection.issuePinsLoadedCount} 个`}
                  </span>
                  {model.issueSection.issuePinsError && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => void model.issueSection?.onRetryIssuePins()}
                    >
                      重试
                    </Button>
                  )}
                </div>
              )}
            {model.issueSection?.stageKind === "video" &&
              model.issueSection.issueNavigation.status !== "idle" && (
                <div
                  className="absolute bottom-20 right-4 z-workbench-top flex max-w-sm items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs text-foreground shadow-lg"
                  role="status"
                  aria-live="polite"
                  data-testid="issue-frame-navigation"
                  data-status={model.issueSection.issueNavigation.status}
                  data-frame-index={model.issueSection.issueNavigation.frameIndex ?? ""}
                  data-workbench-issue-navigation
                >
                  <span>
                    {model.issueSection.issueNavigation.message ??
                      (model.issueSection.issueNavigation.status === "preparing"
                        ? `正在准备源帧 F ${model.issueSection.issueNavigation.frameIndex}…`
                        : model.issueSection.issueNavigation.status === "ready"
                          ? `已定位源帧 F ${model.issueSection.issueNavigation.frameIndex}`
                          : model.issueSection.issueNavigation.status === "timeout"
                            ? "源帧准备超时，原锚点已保留"
                            : model.issueSection.issueNavigation.status === "cancelled"
                              ? "定位已取消，原锚点已保留"
                              : "源帧暂不可用，原锚点已保留")}
                  </span>
                  {model.issueSection.issueNavigation.status !== "preparing" &&
                    model.issueSection.issueNavigation.status !== "ready" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        data-testid="issue-frame-retry"
                        onClick={() => void model.issueSection?.onRetryIssueNavigation()}
                      >
                        重试
                      </Button>
                    )}
                </div>
              )}
          </>
        }
      />
      {model.issueSection &&
        (() => {
          const fabShown = fabRevealed || model.issueSection.issuePinDropArmed;
          const hiddenCls = fabShown ? false : FAB_HIDDEN_CLASS;
          const count = model.issueSection.openIssueCount;
          const countLabel = model.issueSection.openIssueCountError
            ? "数量暂不可用"
            : model.issueSection.openIssueCountLoading
              ? "数量加载中"
              : count === null
                ? "数量未知"
                : `${count} 个未解决`;
          return (
            <>
              <button
                type="button"
                aria-label={`查看问题（${countLabel}）`}
                title={`本任务问题：${countLabel}`}
                onClick={model.issueSection.onOpenList}
                className={cn(ISSUE_FAB_CLASS, "bottom-20", hiddenCls)}
                data-testid="issue-fab"
                data-workbench-issue-navigation
                data-workbench-fab
              >
                <Icon name="flag" size={14} />
                {(count === null || count > 0) && (
                  <span className="absolute -right-1 -top-1 min-w-4 rounded-[10px] bg-status-caution-soft px-1.5 py-px text-center text-2xs text-status-caution">
                    {model.issueSection.openIssueCountError ? "?" : (count ?? "…")}
                  </span>
                )}
              </button>
              {(model.issueSection.stageKind === "image" ||
                model.issueSection.stageKind === "video") && (
                <button
                  type="button"
                  aria-label={
                    model.issueSection.issuePinDropArmed
                      ? "取消像素 issue 落点模式"
                      : "进入像素 issue 落点模式"
                  }
                  title={
                    model.issueSection.issuePinDropArmed
                      ? "再次点击取消"
                      : "单击画布落点创建像素 issue"
                  }
                  onClick={model.issueSection.onToggleIssuePinDrop}
                  className={cn(
                    ISSUE_FAB_CLASS,
                    "bottom-32",
                    model.issueSection.issuePinDropArmed &&
                      "border-status-caution text-status-caution",
                    hiddenCls,
                  )}
                  data-testid="issue-pin-fab"
                  data-workbench-issue-navigation
                  data-workbench-fab
                  data-armed={model.issueSection.issuePinDropArmed ? "true" : "false"}
                >
                  <Icon name="crosshair" size={14} />
                </button>
              )}
              <IssueCreateModal {...model.issueSection.createModal} />
            </>
          );
        })()}
    </>
  );
}
