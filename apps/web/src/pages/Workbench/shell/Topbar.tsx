import { useEffect, useRef, useState, lazy, Suspense } from "react";
import { Button } from "@/components/ui/Button";
import { Icon, type IconName } from "@/components/ui/Icon";
import { DropdownMenu, type DropdownItem } from "@/components/ui/DropdownMenu";
import { AssigneeAvatarStack } from "@/components/ui/AssigneeAvatarStack";
import { SkipTaskModal, type SkipReason } from "./SkipTaskModal";
import { BatchStatusBadge } from "@/components/badges/BatchStatusBadge";
import { useWhatsNewStore } from "@/components/shell/WhatsNewDialog";
import { appVersion } from "@/utils/releaseNotes";
import { useTheme } from "@/hooks/useTheme";
import { useBugDrawerStore } from "@/stores/bugDrawerStore";
import type { TaskResponse } from "@/types";
import type { VideoSegment } from "@/api/videoTracker";
import type { GuardedNavigate } from "@/utils/workbenchNavigation";
import type { WorkspaceSide, WorkspaceSideState } from "../layout/workbenchLayoutExecutor";

// 与主界面 TopBar 共用同一懒加载通知入口；工作台传入自己的导航回调。
const NotificationsPopover = lazy(() =>
  import("@/components/shell/NotificationsPopover").then((m) => ({
    default: m.NotificationsPopover,
  })),
);

interface TopbarProps {
  /** 项目名 + 展示 ID（如 P-0001）；显示在左侧 task id 前作为项目上下文。 */
  projectName: string;
  projectDisplayId: string;
  task: TaskResponse | undefined;
  taskIdx: number;
  taskTotal: number;
  aiRunning: boolean;
  /** v0.9.6 · 当前任务所属批次状态;pre_annotated 时显示「AI 预标已就绪」紫徽章. */
  batchStatus?: string;
  isSubmitting: boolean;
  /** 当前置信度阈值（0~1）；变化时短暂浮出反馈，给 [ ] 盲调用。 */
  confThreshold?: number;
  onShowHotkeys: () => void;
  onBack?: () => void;
  onToggleSide?: (side: WorkspaceSide) => void;
  sides?: Record<WorkspaceSide, WorkspaceSideState>;
  /** 工作台标注指引入口；由布局层按当前项目作用域提供。 */
  guideSlot?: React.ReactNode;
  layoutMenuSlot?: React.ReactNode;
  layoutDisabled?: boolean;
  onRunAi?: () => void;
  aiOpen?: boolean;
  aiDisabled?: boolean;
  /** 视频任务的画布级 AI 追踪入口。 */
  onToggleTracker?: () => void;
  trackerOpen?: boolean;
  trackerRunning?: boolean;
  onPrev: () => void;
  onNext: () => void;
  onSubmit: () => void;
  submitDisabled?: boolean;
  onSmartNextOpen?: () => void;
  onSmartNextUncertain?: () => void;
  /** v0.15.3 · 齿轮图标直接打开设置窗口;缺省不渲染该项。 */
  onOpenWorkbenchSettings?: () => void;
  /** v0.6.5 状态机：审核中可撤回 / 已通过可重开。 */
  canWithdraw?: boolean;
  canReopen?: boolean;
  isWithdrawing?: boolean;
  isReopening?: boolean;
  onWithdraw?: () => void;
  onReopen?: () => void;
  // v0.8.7 F7 · 任务跳过；缺省时不渲染按钮
  isSkipping?: boolean;
  onSkip?: (reason: SkipReason, note?: string) => void;
  /** M2 · review 模式专属操作 */
  mode?: "annotate" | "review";
  onApprove?: () => void;
  onReject?: () => void;
  isApproving?: boolean;
  isRejecting?: boolean;
  /** M2 · review 模式下 Topbar 左侧附加插槽（ReviewerMiniPanel chip） */
  reviewInfoSlot?: React.ReactNode;
  videoSegments?: VideoSegment[];
  activeVideoSegmentId?: string | null;
  onSelectVideoSegment?: (segmentId: string | null) => void;
  submitLabel?: string;
  /**
   * 通知面板导航回调：工作台在真正改路由前跑视频/Mask 离开检查。
   * 缺省时不渲染通知入口（测试/故事用 Topbar 时保持原行为）。
   */
  onNotificationNavigate?: GuardedNavigate;
}

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

/**
 * Topbar 三段（v0.5.3）：
 * - 左：标题 / 索引（task.display_id · file_name · n / total）
 * - 中：上一题 / 提交 / 下一题 / ⌄ 智能切题
 * - 右：阈值反馈浮 + AI + 主题 + ⋯ 溢出菜单（? 帮助 + 设置）
 *
 * 工具切换 → ToolDock（左侧垂直）；撤销/重做/缩放/适应 → FloatingDock（画布右下）。
 */
export function Topbar({
  projectName,
  projectDisplayId,
  task,
  taskIdx,
  taskTotal,
  aiRunning,
  batchStatus,
  isSubmitting,
  confThreshold,
  onShowHotkeys,
  onBack,
  onToggleSide,
  sides = { left: "empty", right: "empty" },
  guideSlot,
  layoutMenuSlot,
  layoutDisabled = false,
  onRunAi,
  aiOpen = false,
  aiDisabled = false,
  onToggleTracker,
  trackerOpen = false,
  trackerRunning = false,
  onPrev,
  onNext,
  onSubmit,
  submitDisabled = false,
  onSmartNextOpen,
  onSmartNextUncertain,
  onOpenWorkbenchSettings,
  canWithdraw = false,
  canReopen = false,
  isWithdrawing = false,
  isReopening = false,
  onWithdraw,
  onReopen,
  isSkipping = false,
  onSkip,
  mode = "annotate",
  onApprove,
  onReject,
  isApproving = false,
  isRejecting = false,
  reviewInfoSlot,
  videoSegments,
  activeVideoSegmentId,
  onSelectVideoSegment,
  submitLabel = "提交",
  onNotificationNavigate,
}: TopbarProps) {
  const { resolved, setTheme } = useTheme();
  const openBugDrawer = useBugDrawerStore((state) => state.openDrawer);
  // v0.8.7 F7 · 跳过任务 modal 状态
  const [skipOpen, setSkipOpen] = useState(false);
  const status = task?.status;
  const isReview = status === "review";
  const isCompleted = status === "completed";
  // 阈值变化时浮出 1.5s 数值反馈（[ ] 键盲调反馈）
  const [showThr, setShowThr] = useState(false);
  const lastThrRef = useRef<number | undefined>(confThreshold);
  useEffect(() => {
    if (confThreshold === undefined) return;
    if (lastThrRef.current === undefined) {
      lastThrRef.current = confThreshold;
      return;
    }
    if (Math.abs(confThreshold - lastThrRef.current) < 1e-6) return;
    lastThrRef.current = confThreshold;
    setShowThr(true);
    const t = setTimeout(() => setShowThr(false), 1500);
    return () => clearTimeout(t);
  }, [confThreshold]);

  const indexLabel = taskTotal > 0 && taskIdx >= 0 ? `${taskIdx + 1} / ${taskTotal}` : "";

  const smartItems: DropdownItem[] = [];
  if (onSmartNextOpen)
    smartItems.push({ id: "next-open", label: "下一未标注", kbd: "N", onSelect: onSmartNextOpen });
  if (onSmartNextUncertain)
    smartItems.push({
      id: "next-uncertain",
      label: "下一最不确定",
      kbd: "U",
      onSelect: onSmartNextUncertain,
    });

  const nextTheme = resolved === "dark" ? "light" : "dark";
  const themeIcon: IconName = nextTheme === "dark" ? "moon" : "sun";
  const themeActionLabel = nextTheme === "dark" ? "切到夜间" : "切到日间";
  const themeTitle = `当前${resolved === "dark" ? "夜间" : "日间"}，${themeActionLabel}`;

  const videoSegmentSelect = videoSegments && onSelectVideoSegment && (
    <select
      aria-label="当前视频分段"
      value={activeVideoSegmentId ?? ""}
      onChange={(event) => onSelectVideoSegment(event.target.value || null)}
      onKeyDown={(event) => {
        // Keep native select keys out of the overflow menu's item navigation.
        if (["ArrowUp", "ArrowDown", "Home", "End", "Enter", " "].includes(event.key)) {
          event.stopPropagation();
        }
      }}
      className="h-7 min-w-0 w-32 max-w-full rounded border border-border bg-card px-2 text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring @max-[1000px]:hidden"
    >
      <option value="">选择分段</option>
      {videoSegments.map((segment) => (
        <option key={segment.id} value={segment.id} disabled={segment.status === "completed"}>
          S{segment.segment_index + 1} · {segment.start_frame}-{segment.end_frame} ·{" "}
          {segment.status}
          {segment.assignee_id ? ` · ${segment.assignee_id.slice(0, 8)}` : " · 未分配"}
        </option>
      ))}
    </select>
  );

  return (
    <div
      data-testid="workbench-topbar"
      className="@container shrink-0 min-w-0 bg-card border-b border-border"
    >
      <div className="relative flex items-center gap-3 px-4 py-2 @max-[700px]:gap-1 @max-[700px]:px-2">
        {/* 左：标题段 — display_id 主、文件名次、索引徽章右贴 */}
        <div className="flex flex-initial items-center gap-2.5 min-w-0 max-w-[360px] @max-[1100px]:flex-none @max-[1100px]:gap-0">
          <div className="flex shrink-0 items-center gap-0.5">
            {onBack && (
              <Button
                variant="ghost"
                size="sm"
                onClick={onBack}
                title="返回"
                aria-label="返回"
                className="px-2 py-1 text-muted-foreground @max-[1100px]:w-7 @max-[1100px]:p-0"
              >
                <Icon name="chevLeft" size={13} />
                <span className="@max-[1100px]:hidden">返回</span>
              </Button>
            )}
            {onToggleSide && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onToggleSide("left")}
                disabled={layoutDisabled || sides.left === "empty"}
                title={
                  sides.left === "empty"
                    ? "画布左侧没有停靠面板"
                    : `${sides.left === "open" ? "收起" : "展开"}画布左侧所有面板`
                }
                aria-label="左侧面板"
                aria-expanded={sides.left === "open"}
                className="justify-center w-7 h-7 p-0 border-transparent rounded-[var(--radius-sm)] bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground"
              >
                <Icon name="panelLeft" size={14} />
              </Button>
            )}
          </div>
          <span className="shrink-0 w-px h-[18px] bg-border @max-[1100px]:hidden" />
          <span
            className="flex-auto min-w-0 overflow-hidden text-sm font-semibold text-foreground truncate @max-[1100px]:hidden"
            title={projectName}
          >
            {projectName}
          </span>
        </div>

        {/* 中：任务标识 + 任务导航 + 状态相关主操作（整体居中）。
             `min-w-max` 让中段永不压缩到内容以下；空间不足时由左侧项目名先收缩，
             避免 shrink-0 的元数据/按钮溢出压到相邻控件（见 workbench-topbar.spec）。 */}
        <div className="flex flex-1 min-w-max items-center justify-center gap-1.5 [&>button]:shrink-0 @max-[1000px]:[&>button]:w-7 @max-[1000px]:[&>button]:p-0 @max-[700px]:gap-0.5">
          <div className="flex items-center gap-2.5 min-w-0 @max-[700px]:hidden">
            <span className="mono shrink-0 text-xs text-muted-foreground @max-[1400px]:hidden">
              {projectDisplayId}
            </span>
            <span className="shrink-0 w-px h-4 bg-border @max-[1400px]:hidden" />
            <span className="mono shrink-0 text-sm font-semibold text-foreground">
              {task?.display_id ?? "—"}
            </span>
            <span
              className="min-w-0 max-w-[220px] overflow-hidden text-sm text-muted-foreground truncate @max-[900px]:hidden"
              title={task?.file_name ?? undefined}
            >
              {task?.file_name ?? "—"}
            </span>
            {indexLabel && (
              <span className="mono shrink-0 px-2 py-0.5 text-xs font-medium text-muted-foreground tracking-[0.2px] bg-muted border border-border rounded-full @max-[900px]:hidden">
                {indexLabel}
              </span>
            )}
            <div className="flex shrink-0 items-center gap-2.5 @max-[1400px]:hidden">
              {/* v0.9.6 · 仅 pre_annotated 时显示徽章, 标注员一眼知道「先看 AI 候选」 */}
              {batchStatus === "pre_annotated" && <BatchStatusBadge status="pre_annotated" />}
              {/* v0.7.2 · 责任人胶囊：标注员 / 审核员（list_tasks/get_task 已 populate） */}
              {(task?.assignee || task?.reviewer) && (
                <span className="shrink-0 w-px h-4 bg-border" />
              )}
              {task?.assignee && (
                <AssigneeAvatarStack users={[task.assignee]} label="标注" max={1} />
              )}
              {task?.reviewer && (
                <AssigneeAvatarStack users={[task.reviewer]} label="审核" max={1} />
              )}
            </div>
            {videoSegmentSelect}
          </div>
          <span className="shrink-0 w-px h-4 bg-border @max-[700px]:hidden" />
          <Button
            size="sm"
            onClick={onPrev}
            title="上一个任务"
            aria-label="上一个任务"
            className="w-7 p-0"
          >
            <Icon name="chevLeft" size={13} />
          </Button>
          {mode === "review" ? (
            <>
              <Button
                variant="primary"
                size="sm"
                onClick={onApprove}
                disabled={isApproving || !onApprove}
                aria-label="通过"
                data-testid="review-approve"
                title="通过 (A)"
              >
                <Icon name="check" size={13} />
                <span className="@max-[1000px]:hidden">通过</span>
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={onReject}
                disabled={isRejecting || !onReject}
                aria-label="退回"
                data-testid="review-reject"
                title="退回 (R)"
              >
                <Icon name="x" size={12} />
                <span className="@max-[1000px]:hidden">退回</span>
              </Button>
            </>
          ) : isReview ? (
            <Button
              variant="primary"
              size="sm"
              onClick={onWithdraw}
              disabled={!canWithdraw || isWithdrawing || !onWithdraw}
              aria-label="撤回提交"
              title={canWithdraw ? "撤回提交，回到编辑态" : "审核员已介入，无法撤回"}
            >
              <Icon name="chevLeft" size={13} />
              <span className="@max-[1000px]:hidden">撤回提交</span>
            </Button>
          ) : isCompleted ? (
            <Button
              variant="primary"
              size="sm"
              onClick={onReopen}
              disabled={!canReopen || isReopening || !onReopen}
              aria-label="继续编辑"
              title="重开任务，回到编辑态"
            >
              <Icon name="edit" size={13} />
              <span className="@max-[1000px]:hidden">继续编辑</span>
            </Button>
          ) : (
            <>
              <Button
                variant="primary"
                size="sm"
                onClick={onSubmit}
                disabled={isSubmitting || submitDisabled}
                title={submitLabel}
                aria-label={submitLabel}
                data-testid="workbench-submit"
              >
                <Icon name="check" size={13} />
                <span className="@max-[1000px]:hidden">{submitLabel}</span>
              </Button>
              {onSkip && (
                <Button
                  size="sm"
                  onClick={() => setSkipOpen(true)}
                  disabled={isSkipping || isSubmitting}
                  title="图像损坏 / 无目标 / 不清晰时跳过本题"
                  aria-label="跳过"
                  data-testid="workbench-skip"
                >
                  <Icon name="x" size={12} />
                  <span className="@max-[1000px]:hidden">跳过</span>
                </Button>
              )}
            </>
          )}
          <Button
            size="sm"
            onClick={onNext}
            title="下一个任务"
            aria-label="下一个任务"
            className="w-7 p-0"
          >
            <Icon name="chevRight" size={13} />
          </Button>

          <div className="flex shrink-0 items-center gap-1.5 @max-[1000px]:hidden">
            {smartItems.length > 0 && (
              <DropdownMenu
                items={smartItems}
                trigger={({ toggle, ref, open }) => (
                  <Button
                    ref={ref}
                    variant="ghost"
                    size="sm"
                    onClick={toggle}
                    title="智能切题 (N / U)"
                    className={cn("px-1 py-1 text-muted-foreground ml-0.5", open && "bg-muted")}
                  >
                    <Icon name="wandSparkles" size={13} />
                  </Button>
                )}
              />
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={onShowHotkeys}
              title="快捷键 (?)"
              aria-label="快捷键"
              className="mono px-1 py-1 text-muted-foreground"
            >
              ?
            </Button>
          </div>
        </div>
        {/* 右：AI 主操作（annotate）或 ReviewerMini chip（review）+ 溢出菜单 */}
        <div className="relative flex shrink-0 items-center justify-end gap-1.5 [&>button]:shrink-0 [&>button]:whitespace-nowrap @max-[700px]:gap-0.5">
          <div className="@max-[1100px]:hidden">{reviewInfoSlot}</div>
          {showThr && confThreshold !== undefined && (
            <span className="mono absolute top-[calc(100%+6px)] right-0 z-local-overlay inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-status-info pointer-events-none bg-status-info-soft border border-violet-500/30 rounded-full shadow-md">
              <span className="w-1.5 h-1.5 bg-violet-600 dark:bg-violet-400 rounded-full" />
              阈值 {(confThreshold * 100).toFixed(0)}%
            </span>
          )}
          {mode === "annotate" && (
            <>
              {onToggleTracker && (
                <Button
                  variant="ai"
                  size="sm"
                  onClick={onToggleTracker}
                  aria-label="发现新目标"
                  aria-pressed={trackerOpen}
                  title={
                    trackerOpen
                      ? "聚焦画布级多目标追踪"
                      : "发现或播种多个新目标，不延展当前选中轨迹"
                  }
                  className="h-7 px-3 @max-[1100px]:w-7 @max-[1100px]:p-0"
                  data-testid="workbench-ai-tracker"
                >
                  {trackerRunning ? (
                    <Icon
                      name="loader2"
                      size={13}
                      className="animate-spin motion-reduce:animate-none"
                    />
                  ) : (
                    <Icon name="bot" size={13} />
                  )}
                  <span className="@max-[1100px]:hidden">发现目标</span>
                </Button>
              )}
              {onRunAi && (
                <Button
                  variant="ai"
                  size="sm"
                  onClick={onRunAi}
                  aria-label="AI 单题"
                  aria-pressed={aiOpen}
                  disabled={aiDisabled}
                  title={aiOpen ? "聚焦 AI 单题面板" : "打开 AI 单题面板"}
                  className="h-7 px-3 @max-[1100px]:w-7 @max-[1100px]:p-0"
                  data-testid="workbench-ai-single"
                >
                  {aiRunning ? (
                    <Icon
                      name="loader2"
                      size={13}
                      className="animate-spin motion-reduce:animate-none"
                    />
                  ) : (
                    <Icon name="wandSparkles" size={13} />
                  )}
                  <span className="@max-[1100px]:hidden">AI</span>
                </Button>
              )}
            </>
          )}
          {guideSlot && <div className="flex shrink-0 items-center">{guideSlot}</div>}
          {onNotificationNavigate && (
            <Suspense fallback={null}>
              <NotificationsPopover navigate={onNotificationNavigate} />
            </Suspense>
          )}
          <div className="flex items-center gap-1.5 @max-[1000px]:hidden">
            {/* 与主界面 TopBar 一致:版本号可点击,随时查看当前版本更新内容。
                窄屏工作台顶栏已无横向余量(见 workbench-topbar.spec 的防重叠断言),
                故随中段压缩一并收起;<1000px 时改由「更多工具」菜单提供入口。 */}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => useWhatsNewStore.getState().openManually()}
              title="查看本次更新内容"
              aria-label={`查看 v${appVersion} 更新内容`}
              data-testid="workbench-whats-new"
              className="mono px-1.5 py-1 text-muted-foreground @max-[900px]:hidden"
            >
              v{appVersion}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => openBugDrawer()}
              title="报告 Bug / 提交反馈"
              aria-label="报告 Bug / 提交反馈"
              data-testid="workbench-report-bug"
              className="justify-center w-7 h-7 p-0 text-muted-foreground bg-transparent border-transparent rounded-[var(--radius-sm)] shadow-none hover:text-foreground hover:bg-muted"
            >
              <Icon name="bug" size={14} />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setTheme(nextTheme)}
              title={themeTitle}
              aria-label={themeTitle}
              className="justify-center w-7 h-7 p-0 text-muted-foreground bg-transparent border-transparent rounded-[var(--radius-sm)] shadow-none hover:text-foreground hover:bg-muted"
            >
              <Icon name={themeIcon} size={14} />
            </Button>

            {onOpenWorkbenchSettings && (
              <Button
                variant="ghost"
                size="sm"
                onClick={onOpenWorkbenchSettings}
                title="工作台设置"
                aria-label="工作台设置"
                className="justify-center w-7 h-7 p-0 text-muted-foreground bg-transparent border-transparent rounded-[var(--radius-sm)] shadow-none hover:text-foreground hover:bg-muted"
              >
                <Icon name="settings" size={14} />
              </Button>
            )}
          </div>
          <div className="hidden @max-[1000px]:block">
            <DropdownMenu
              footer={videoSegmentSelect}
              items={[
                ...smartItems,
                { id: "hotkeys", label: "快捷键", onSelect: onShowHotkeys },
                { id: "theme", label: themeActionLabel, onSelect: () => setTheme(nextTheme) },
                {
                  id: "whats-new",
                  label: `本次更新 (v${appVersion})`,
                  onSelect: () => useWhatsNewStore.getState().openManually(),
                },
                { id: "bug-report", label: "报告 Bug / 提交反馈", onSelect: openBugDrawer },
                ...(onOpenWorkbenchSettings
                  ? [{ id: "settings", label: "工作台设置", onSelect: onOpenWorkbenchSettings }]
                  : []),
              ]}
              trigger={({ ref, toggle, open }) => (
                <Button
                  ref={ref}
                  size="sm"
                  variant="ghost"
                  className="w-7 p-0"
                  title="更多工具"
                  aria-label="更多工具"
                  aria-haspopup="menu"
                  aria-expanded={open}
                  onClick={toggle}
                >
                  <Icon name="more" size={14} />
                </Button>
              )}
            />
          </div>
          {layoutMenuSlot}
          {onToggleSide && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onToggleSide("right")}
              disabled={layoutDisabled || sides.right === "empty"}
              title={
                sides.right === "empty"
                  ? "画布右侧没有停靠面板"
                  : `${sides.right === "open" ? "收起" : "展开"}画布右侧所有面板`
              }
              aria-label="右侧面板"
              aria-expanded={sides.right === "open"}
              className="justify-center w-7 h-7 p-0 border-transparent rounded-[var(--radius-sm)] bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground"
            >
              <Icon name="panelRight" size={14} />
            </Button>
          )}
        </div>
      </div>
      {onSkip && (
        <SkipTaskModal
          open={skipOpen}
          isSubmitting={isSkipping}
          onClose={() => setSkipOpen(false)}
          onConfirm={(reason, note) => {
            setSkipOpen(false);
            onSkip(reason, note);
          }}
        />
      )}
    </div>
  );
}
