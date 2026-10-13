import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Button } from "@/components/ui/Button";
import { Icon, type IconName } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { Thumbnail } from "@/components/Thumbnail";
import type { TaskResponse } from "@/types";
import type { ClassesConfig } from "@/api/projects";
import type { BatchResponse } from "@/api/batches";
import { ClassPalette } from "./ClassPalette";
import {
  ContinuousCreationControls,
  type ContinuousCreationControlsProps,
} from "./ContinuousCreationControls";
import { ResizeHandle } from "./ResizeHandle";
import type { BboxCreationMode } from "../stage/ImageStage.helpers";

const PALETTE_HEIGHT_KEY = "workbench.leftPalette.height";
const PALETTE_HEIGHT_DEFAULT = 220;
const PALETTE_HEIGHT_MIN = 112;
const PALETTE_HEIGHT_MAX = 420;

interface TaskQueuePanelProps {
  open: boolean;
  classes: string[];
  classesConfig?: ClassesConfig;
  /** 当前激活工具的显示名 + 图标，色板图例据此标明类别归属的工具。 */
  toolLabel: string;
  toolIcon: IconName;
  activeClass: string;
  recentClasses?: string[];
  tasks: TaskResponse[];
  taskId: string | undefined;
  taskIdx: number;
  hasNextPage: boolean | undefined;
  isFetchingNextPage: boolean;
  onFetchNextPage: () => void;
  onSelectTask: (id: string) => void;
  batches?: BatchResponse[];
  selectedBatchId: string | null;
  onSelectBatch?: (batchId: string | null) => void;
  totalCount?: number;
  isOwner?: boolean;
  onGoToBatchSettings?: () => void;
  /** 受控宽度（仅 open=true 生效）。 */
  width: number;
  onResize: (w: number) => void;
  /** 拖拽 handle 的像素边界与双击重置值(随窗口宽度动态变化;不传按内置默认)。 */
  widthMin?: number;
  widthMax?: number;
  widthResetTo?: number;
  detachedQueue?: boolean;
  detachedPalette?: boolean;
  onDetachQueue?: () => void;
  onDetachPalette?: () => void;
  floatingSection?: "queue" | "palette";
  /**
   * v0.13.3-5 · 左栏色板默认是只读图例(2D 落框时弹窗/数字键选类)。点云 3D 台没有落框弹窗,
   * 放置新框直接取 activeClass,故 3D 下让色板可点选(classPickable),点击即设 activeClass。
   */
  classPickable?: boolean;
  onPickClass?: (cls: string) => void;
  continuousCreation?: ContinuousCreationControlsProps;
  bboxCreation?: {
    mode: BboxCreationMode;
    onChange: (mode: BboxCreationMode) => void;
    disabled?: boolean;
  };
}

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

function readPaletteHeight(): number {
  if (typeof window === "undefined") return PALETTE_HEIGHT_DEFAULT;
  const raw = Number(window.localStorage.getItem(PALETTE_HEIGHT_KEY));
  if (!Number.isFinite(raw) || raw <= 0) return PALETTE_HEIGHT_DEFAULT;
  return Math.max(PALETTE_HEIGHT_MIN, Math.min(PALETTE_HEIGHT_MAX, Math.round(raw)));
}

function statusClassName(task: TaskResponse): string {
  if (task.status === "completed") return "text-status-positive";
  if (task.status === "review") return "text-status-caution";
  if (task.status === "rejected") return "text-status-danger";
  if (task.total_annotations > 0) return "text-brand";
  if (task.total_predictions > 0) return "text-status-info";
  return "text-muted-foreground";
}

function TaskItem({
  task,
  isActive,
  onSelect,
}: {
  task: TaskResponse;
  isActive: boolean;
  onSelect: () => void;
}) {
  const isLocked = task.status === "review" || task.status === "completed";
  const isRejected = task.status === "rejected";
  const statusLabel =
    task.status === "completed"
      ? "已完成"
      : task.status === "review"
        ? "待审核"
        : task.status === "rejected"
          ? "待重做"
          : task.total_annotations > 0
            ? "进行中"
            : task.total_predictions > 0
              ? "AI 已预标"
              : "未开始";
  const markerClassName = cn(
    "absolute left-0.5 top-2 bottom-2 w-[3px] rounded-[2px]",
    isActive ? "bg-brand" : isRejected && "bg-rose-600 dark:bg-rose-400",
  );

  return (
    <div
      onClick={onSelect}
      className={cn(
        "relative flex items-center gap-2.5 my-[3px] px-3 py-2.5 rounded-[var(--radius-md)] cursor-pointer transition-[background,border-color] duration-[0.12s]",
        isActive
          ? "border border-brand/30 bg-brand/10"
          : isRejected
            ? "border border-rose-500/25 bg-rose-500/6"
            : "border border-transparent bg-transparent",
        !isActive && "hover:bg-muted",
      )}
    >
      {(isActive || isRejected) && <span aria-hidden className={markerClassName} />}
      <Thumbnail src={task.thumbnail_url} blurhash={task.blurhash} width={40} height={40} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-1">
          <span
            className={cn(
              "mono text-xs font-semibold",
              isActive ? "text-brand" : "text-foreground",
            )}
          >
            {task.display_id}
          </span>
          <div className="flex items-center gap-1">
            {isLocked && (
              <span
                title={task.status === "review" ? "已提交质检 · 已锁定" : "已通过审核 · 已锁定"}
                className="inline-flex text-muted-foreground"
              >
                <Icon name="lock" size={11} />
              </span>
            )}
            {task.total_annotations > 0 && (
              <span className="inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-brand/10 text-brand text-2xs font-medium whitespace-nowrap">
                {task.total_annotations}
              </span>
            )}
          </div>
        </div>
        <div className="mt-0.5 overflow-hidden text-muted-foreground text-xs truncate">
          {task.file_name}
        </div>
        <div
          className={cn(
            "inline-flex items-center gap-1 mt-[3px] text-2xs font-medium",
            statusClassName(task),
          )}
        >
          <span className="w-[5px] h-[5px] rounded-full bg-current" />
          {statusLabel}
        </div>
      </div>
    </div>
  );
}

function VirtualInner({ height, children }: { height: number; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    ref.current?.style.setProperty("height", `${height}px`);
  }, [height]);

  return (
    <div ref={ref} className="relative">
      {children}
    </div>
  );
}

function VirtualRow({
  start,
  dataIndex,
  measureElement,
  children,
}: {
  start: number;
  dataIndex?: number;
  measureElement?: (node: HTMLDivElement | null) => void;
  children: ReactNode;
}) {
  const ref = useCallback(
    (node: HTMLDivElement | null) => {
      if (node) {
        node.style.setProperty("transform", `translateY(${start}px)`);
      }
      measureElement?.(node);
    },
    [measureElement, start],
  );

  return (
    <div ref={ref} data-index={dataIndex} className="absolute top-0 left-0 w-full">
      {children}
    </div>
  );
}

export function TaskQueuePanel({
  open,
  classes,
  classesConfig,
  toolLabel,
  toolIcon,
  activeClass,
  recentClasses,
  tasks,
  taskId,
  taskIdx,
  hasNextPage,
  isFetchingNextPage,
  onFetchNextPage,
  onSelectTask,
  batches,
  selectedBatchId,
  onSelectBatch,
  totalCount,
  isOwner,
  onGoToBatchSettings,
  width,
  onResize,
  widthMin = 200,
  widthMax = 560,
  widthResetTo,
  detachedQueue = false,
  detachedPalette = false,
  onDetachQueue,
  onDetachPalette,
  floatingSection,
  classPickable = false,
  onPickClass,
  continuousCreation,
  bboxCreation,
}: TaskQueuePanelProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  const [paletteHeight, setPaletteHeight] = useState(readPaletteHeight);
  const floating = Boolean(floatingSection);
  const showQueue = floatingSection ? floatingSection === "queue" : !detachedQueue;
  const showPalette = floatingSection ? floatingSection === "palette" : !detachedPalette;

  const onPaletteResize = useCallback((next: number) => {
    const clamped = Math.max(PALETTE_HEIGHT_MIN, Math.min(PALETTE_HEIGHT_MAX, Math.round(next)));
    setPaletteHeight(clamped);
    try {
      window.localStorage.setItem(PALETTE_HEIGHT_KEY, String(clamped));
    } catch {
      /* noop */
    }
  }, []);

  // rejected 任务置顶，其余保持原序
  const sortedTasks = useMemo(() => {
    const rejected = tasks.filter((t) => t.status === "rejected");
    const rest = tasks.filter((t) => t.status !== "rejected");
    return [...rejected, ...rest];
  }, [tasks]);

  const rejectedCount = useMemo(() => tasks.filter((t) => t.status === "rejected").length, [tasks]);
  const activeTaskIndex = useMemo(
    () => sortedTasks.findIndex((t) => t.id === taskId),
    [sortedTasks, taskId],
  );

  const virtualizer = useVirtualizer({
    count: sortedTasks.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 88,
    overscan: 5,
  });

  // 滚到接近末尾时触发加载下一页
  useEffect(() => {
    if (!showQueue) return;
    const virtualItems = virtualizer.getVirtualItems();
    if (!virtualItems.length) return;
    const last = virtualItems[virtualItems.length - 1];
    if (last.index >= sortedTasks.length - 10 && hasNextPage && !isFetchingNextPage) {
      onFetchNextPage();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showQueue, virtualizer.getVirtualItems(), hasNextPage, isFetchingNextPage]);

  useEffect(() => {
    if (!showQueue) return;
    if (!open || activeTaskIndex < 0) return;
    const frame = window.requestAnimationFrame(() => {
      virtualizer.scrollToIndex(activeTaskIndex, { align: "center" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open, showQueue, activeTaskIndex, virtualizer]);

  // 高度用 useElementStyle 注入 CSS 变量:本面板在收起左栏后会条件卸载,再展开时挂成新 DOM;
  // 若仍用 useRef+useEffect([paletteHeight]),依赖未变 effect 不重跑 → 新元素拿不到
  // --left-palette-height → 高度回退到默认(同右栏 B-56「展开收起后回到原位 / 第一次拖拽不跟手」)。
  const rootStyleRef = useElementStyle<HTMLDivElement>(
    useMemo<CSSProperties>(
      () => ({ "--left-palette-height": `${paletteHeight}px` }) as CSSProperties,
      [paletteHeight],
    ),
  );

  if (!open || (!showQueue && !showPalette)) {
    return null;
  }

  return (
    <div
      ref={rootStyleRef}
      className={cn(
        "relative flex flex-col overflow-hidden border-r border-border bg-card",
        floating && "h-full border-r-0",
        showQueue !== showPalette && "[--left-palette-height:auto]",
      )}
    >
      {showQueue && (
        <>
          {batches && batches.length > 0 && onSelectBatch && (
            <FilterGroup
              label="批次"
              compact
              className="grid grid-cols-[auto_minmax(0,1fr)] px-3.5 pt-1.5 pb-0"
            >
              <FilterSelect
                compact
                aria-label="任务批次筛选"
                value={selectedBatchId ?? ""}
                onChange={(e) => onSelectBatch(e.target.value || null)}
                className="w-full"
              >
                <option value="">全部批次（{batches.length}）</option>
                {batches.map((b) => {
                  const statusTag =
                    b.status === "annotating"
                      ? "标注中"
                      : b.status === "active"
                        ? "未开始"
                        : b.status === "rejected"
                          ? "已驳回"
                          : b.status === "draft"
                            ? "草稿"
                            : b.status;
                  return (
                    <option key={b.id} value={b.id}>
                      {b.name} · {statusTag} ({b.completed_tasks}/{b.total_tasks})
                    </option>
                  );
                })}
              </FilterSelect>
            </FilterGroup>
          )}

          {/* v0.6.8 B-15：owner 视角且无任何批次时给出明确入口，避免误以为「100 条就是全部」 */}
          {isOwner && (!batches || batches.length === 0) && onGoToBatchSettings && (
            <div className="flex items-center justify-between gap-2 mx-3.5 mt-1.5 px-2.5 py-2 border border-dashed border-border rounded-[var(--radius-sm)] bg-background text-muted-foreground text-xs">
              <span>未分批次 · 任务统一在「未归类」</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={onGoToBatchSettings}
                className="!px-1.5 !py-0.5 !text-xs"
              >
                前往分批
              </Button>
            </div>
          )}

          {/* v0.7.1 B-15：非 owner 视角且未分到批次 → 显式提示，避免误以为「列表无尽，但只看见 100」 */}
          {!isOwner && (!batches || batches.length === 0) && (
            <div className="mx-3.5 mt-1.5 px-2.5 py-2 border border-dashed border-border rounded-[var(--radius-sm)] bg-background text-muted-foreground text-xs">
              暂未被分派到批次 · 联系项目管理员分配
            </div>
          )}

          <div className="flex items-center justify-between gap-2 px-3.5 pt-2.5 pb-1.5">
            <div className="flex items-center gap-1.5 text-xs font-semibold">
              任务队列
              {selectedBatchId && batches && (
                <span className="text-muted-foreground text-xs font-normal">· 当前批次</span>
              )}
              {rejectedCount > 0 && (
                <span
                  title={`${rejectedCount} 个任务被退回，需重做`}
                  className="inline-flex items-center gap-1 px-1.5 py-px border border-rose-500/30 rounded-full bg-status-danger-soft text-status-danger text-2xs font-semibold"
                >
                  <Icon name="warning" size={10} />
                  {rejectedCount} 待重做
                </span>
              )}
            </div>
            <div className="inline-flex items-center gap-1.5 shrink-0">
              <span
                className="text-muted-foreground text-xs mono"
                title={
                  hasNextPage
                    ? `已加载 ${tasks.length} / 共 ${totalCount ?? tasks.length}（滚动加载更多）`
                    : `共 ${totalCount ?? tasks.length}`
                }
              >
                {taskIdx + 1} / {tasks.length}
                {totalCount != null && totalCount > tasks.length && (
                  <span className="opacity-70"> · 共 {totalCount}</span>
                )}
              </span>
              {onDetachQueue && !floating && (
                <button
                  type="button"
                  className="inline-flex items-center justify-center w-6 h-6 p-0 appearance-none border border-border rounded-[var(--radius-sm)] bg-background text-muted-foreground cursor-pointer hover:border-brand hover:text-brand"
                  onClick={onDetachQueue}
                  title="分离任务队列"
                  aria-label="分离任务队列"
                >
                  <Icon name="pictureInPicture2" size={13} />
                </button>
              )}
            </div>
          </div>

          <div ref={parentRef} className="flex-1 overflow-y-auto px-2 pb-2.5 pt-0">
            <VirtualInner height={virtualizer.getTotalSize()}>
              {virtualizer.getVirtualItems().map((vItem) => {
                const t = sortedTasks[vItem.index];
                if (!t) return null;
                return (
                  <VirtualRow
                    key={vItem.key}
                    start={vItem.start}
                    dataIndex={vItem.index}
                    measureElement={virtualizer.measureElement}
                  >
                    <TaskItem
                      task={t}
                      isActive={t.id === taskId}
                      onSelect={() => onSelectTask(t.id)}
                    />
                  </VirtualRow>
                );
              })}
              {isFetchingNextPage && (
                <VirtualRow start={virtualizer.getTotalSize()}>
                  <div className="px-2.5 py-2 text-muted-foreground text-xs text-center">
                    加载更多...
                  </div>
                </VirtualRow>
              )}
            </VirtualInner>
          </div>
        </>
      )}

      {showPalette && (
        <div
          className={cn(
            "relative overflow-y-auto px-3.5 py-4 pt-4 pb-2.5 border-border",
            showQueue
              ? "flex-[0_1_var(--left-palette-height)] min-h-[112px] max-h-[min(45%,420px)] border-t"
              : "min-h-0 flex-1",
          )}
        >
          {!floating && showQueue && (
            <ResizeHandle
              side="top"
              width={paletteHeight}
              onResize={onPaletteResize}
              min={PALETTE_HEIGHT_MIN}
              max={PALETTE_HEIGHT_MAX}
              resetTo={PALETTE_HEIGHT_DEFAULT}
            />
          )}
          <div className="flex items-center justify-between gap-2 mb-0.5">
            <div className="flex items-center gap-1 text-muted-foreground text-xs">
              <span className="inline-flex items-center gap-1 text-foreground font-semibold">
                <Icon name={toolIcon} size={12} />
                {toolLabel}
              </span>
              <span className="text-muted-foreground">· {classes.length} 个类别</span>
            </div>
            {onDetachPalette && !floating && (
              <button
                type="button"
                className="inline-flex items-center justify-center w-6 h-6 p-0 appearance-none border border-border rounded-[var(--radius-sm)] bg-background text-muted-foreground cursor-pointer hover:border-brand hover:text-brand"
                onClick={onDetachPalette}
                title="分离类别面板"
                aria-label="分离类别面板"
              >
                <Icon name="pictureInPicture2" size={13} />
              </button>
            )}
          </div>
          {bboxCreation && (
            <div className="mb-2 flex items-center gap-2 text-xs" data-testid="bbox-creation-mode">
              <span className="shrink-0 text-muted-foreground">画框起点</span>
              <div className="flex min-w-0 flex-1 gap-1" role="group" aria-label="画框起点">
                {(["corner", "center"] as const).map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    aria-pressed={bboxCreation.mode === mode}
                    disabled={bboxCreation.disabled}
                    onClick={() => bboxCreation.onChange(mode)}
                    title={
                      mode === "corner"
                        ? "从角点拖动；按住 Alt 可临时从中心开始"
                        : "以落点为中心对称展开"
                    }
                    className={cn(
                      "min-w-0 flex-1 rounded-sm border px-2 py-1 text-xs disabled:opacity-50",
                      bboxCreation.mode === mode
                        ? "border-brand bg-brand/10 text-brand"
                        : "border-border bg-background text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {mode === "corner" ? "角点" : "中心"}
                  </button>
                ))}
              </div>
            </div>
          )}
          {continuousCreation ? (
            <ContinuousCreationControls
              {...continuousCreation}
              legend={{ classes, classesConfig, recent: recentClasses, activeClass }}
            />
          ) : (
            <>
              <div className="mb-1.5 text-muted-foreground text-xs">
                {classPickable ? "点击选择放置类别" : "数字键 1-9、0 直接落框时使用"}
              </div>
              <ClassPalette
                classes={classes}
                classesConfig={classesConfig}
                recent={recentClasses}
                activeClass={activeClass}
                enableSearch={classes.length > 9}
                onPick={onPickClass}
                readOnly={!classPickable}
              />
            </>
          )}
        </div>
      )}

      {!floating && (
        <ResizeHandle
          side="right"
          width={width}
          onResize={onResize}
          min={widthMin}
          max={widthMax}
          resetTo={widthResetTo}
        />
      )}
    </div>
  );
}
