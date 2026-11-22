import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import {
  FloatingPanelShell,
  ICON_BUTTON_CLASS,
  type FloatingPanelRect,
} from "./FloatingPanelShell";
import { useDragMove, type FloatingPanelPoint } from "./useDragMove";
import { FLOATING_SELECTION_MAX_SIZE, FLOATING_SELECTION_MIN_SIZE } from "./floatingPanelSizing";
import styles from "./SelectedAnnotationCard.module.css";
import petStyles from "./pet/pet.module.css";

const COLLAPSED_TAB_MIN_W = 116;
const COLLAPSED_TAB_MAX_W = 168;
const COLLAPSED_TAB_H = 40;
const TAB_DRAG_THRESHOLD = 3;
const COLLAPSE_EXIT_MS = 160;

const SHELL_CLASS = "z-popover-elevated";
const PET_LINKED_SHELL_CLASS = `z-overlay-high ring-1 ring-brand/30 shadow-xl ${petStyles.carriedPanel}`;
const TAB_CLASS =
  "group fixed left-[var(--selection-tab-x)] top-[var(--selection-tab-y)] z-popover-elevated grid h-10 w-[var(--selection-tab-w)] cursor-grab touch-none select-none grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-md border border-border bg-card px-2 text-xs text-foreground shadow-lg outline-none transition-[background-color,border-color,box-shadow,transform] duration-200 ease-out hover:-translate-y-px hover:border-brand hover:bg-muted hover:shadow-xl focus-visible:ring-2 focus-visible:ring-ring/60 active:translate-y-0 active:scale-[0.99] motion-reduce:transition-none";
const TAB_DRAGGING_CLASS = "cursor-grabbing border-brand shadow-xl";
const TAB_ICON_CLASS =
  "inline-flex size-6 flex-none items-center justify-center rounded border border-border bg-muted text-brand transition-[border-color,background-color] duration-200 group-hover:border-brand group-hover:bg-card";
const TAB_TITLE_CLASS =
  "min-w-0 overflow-hidden text-ellipsis whitespace-nowrap font-semibold leading-4 text-foreground";
const TAB_ACTION_CLASS =
  "inline-flex size-5 flex-none items-center justify-center rounded border border-border bg-card text-muted-foreground transition-[background-color,border-color,color] duration-200 group-hover:border-brand group-hover:text-brand";
const PANEL_HEADER_CLASS = "inline-flex min-w-0 items-center gap-2";
const PANEL_HEADER_ICON_CLASS =
  "inline-flex size-6 flex-none items-center justify-center rounded border border-brand/30 bg-brand/10 text-brand";
const PANEL_HEADER_TEXT_CLASS = "flex min-w-0 flex-col";
const PANEL_HEADER_META_CLASS = "text-[11px] leading-3 text-muted-foreground";
const PANEL_HEADER_TITLE_CLASS =
  "min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-xs font-semibold text-foreground";
const BODY_CLASS = "px-3 py-2.5";
const SUMMARY_CLASS = "m-0 text-xs leading-[1.5] text-muted-foreground";

type SelectionCardStyle = CSSProperties & {
  "--selection-tab-x"?: string;
  "--selection-tab-y"?: string;
  "--selection-tab-w"?: string;
  "--selection-card-scale-x"?: string;
  "--selection-card-scale-y"?: string;
};

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false)
  );
}

function measureCollapsedTabWidth(title: string): number {
  const titleUnits = Array.from(title.trim() || title).reduce((sum, char) => {
    return sum + (char.charCodeAt(0) > 255 ? 14 : 7);
  }, 0);
  return Math.max(COLLAPSED_TAB_MIN_W, Math.min(COLLAPSED_TAB_MAX_W, 76 + titleUnits));
}

function collapsedTabPositionFromPanel(
  panel: FloatingPanelRect,
  size: { w: number; h: number },
): FloatingPanelPoint {
  return {
    x: Math.round(panel.x + panel.w / 2 - size.w / 2),
    y: Math.round(panel.y + panel.h / 2 - size.h / 2),
  };
}

function panelPositionFromCollapsedTab(
  tabPosition: FloatingPanelPoint,
  panel: FloatingPanelRect,
  size: { w: number; h: number },
): FloatingPanelPoint {
  return {
    x: Math.round(tabPosition.x + size.w / 2 - panel.w / 2),
    y: Math.round(tabPosition.y + size.h / 2 - panel.h / 2),
  };
}

export interface SelectedAnnotationCardProps {
  title: string;
  position: FloatingPanelRect;
  onPositionChange: (patch: Partial<FloatingPanelRect>) => void;
  collapsed: boolean;
  onCollapse: () => void;
  onExpand: () => void;
  linkedToPet?: boolean;
  secondaryBarHidden?: boolean;
  onToggleSecondaryBar?: () => void;
  children: ReactNode;
}

export function SelectedAnnotationCard({
  title,
  position,
  onPositionChange,
  collapsed,
  onCollapse,
  onExpand,
  linkedToPet = false,
  secondaryBarHidden,
  onToggleSecondaryBar,
  children,
}: SelectedAnnotationCardProps) {
  const [renderCollapsed, setRenderCollapsed] = useState(collapsed);
  const [panelLeaving, setPanelLeaving] = useState(false);
  const tabStartRef = useRef<{ x: number; y: number } | null>(null);
  const tabMovedRef = useRef(false);
  const collapsedSize = useMemo(
    () => ({ w: measureCollapsedTabWidth(title), h: COLLAPSED_TAB_H }),
    [title],
  );
  const collapsedPosition = collapsedTabPositionFromPanel(position, collapsedSize);
  const panelMorphStyle = {
    "--selection-card-scale-x": `${collapsedSize.w / position.w}`,
    "--selection-card-scale-y": `${collapsedSize.h / position.h}`,
  } as SelectionCardStyle;
  const tabDrag = useDragMove({
    position: collapsedPosition,
    size: collapsedSize,
    onStart: (pos) => {
      tabStartRef.current = pos;
      tabMovedRef.current = false;
    },
    onChange: (pos) => {
      const start = tabStartRef.current;
      if (
        start &&
        (Math.abs(pos.x - start.x) > TAB_DRAG_THRESHOLD ||
          Math.abs(pos.y - start.y) > TAB_DRAG_THRESHOLD)
      ) {
        tabMovedRef.current = true;
      }
      onPositionChange(panelPositionFromCollapsedTab(pos, position, collapsedSize));
    },
  });

  useEffect(() => {
    if (collapsed === renderCollapsed) {
      setPanelLeaving(false);
      return;
    }
    if (prefersReducedMotion()) {
      setRenderCollapsed(collapsed);
      setPanelLeaving(false);
      return;
    }
    if (collapsed) {
      setPanelLeaving(true);
      const t = window.setTimeout(() => {
        setRenderCollapsed(true);
        setPanelLeaving(false);
      }, COLLAPSE_EXIT_MS);
      return () => window.clearTimeout(t);
    }
    setRenderCollapsed(false);
    setPanelLeaving(false);
  }, [collapsed, renderCollapsed]);

  if (renderCollapsed) {
    return (
      <div
        tabIndex={0}
        data-floating-panel
        aria-label="展开选中信息卡(可拖动)"
        className={cn(TAB_CLASS, styles.collapsedTab, tabDrag.isDragging && TAB_DRAGGING_CLASS)}
        // eslint-disable-next-line no-restricted-syntax -- 收起标签沿用展开卡的记忆位置，经 CSS 变量注入。
        style={
          {
            "--selection-tab-x": `${collapsedPosition.x}px`,
            "--selection-tab-y": `${collapsedPosition.y}px`,
            "--selection-tab-w": `${collapsedSize.w}px`,
          } as SelectionCardStyle
        }
        {...tabDrag.handleProps}
        onClick={() => {
          if (!tabMovedRef.current) onExpand();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onExpand();
          }
        }}
      >
        <span className={TAB_ICON_CLASS} aria-hidden="true">
          <Icon name="tag" size={14} />
        </span>
        <span className={TAB_TITLE_CLASS}>{title}</span>
        <span className={TAB_ACTION_CLASS} aria-hidden="true">
          <Icon name="chevUp" size={13} />
        </span>
      </div>
    );
  }

  return (
    <FloatingPanelShell
      title={title}
      position={position}
      onPositionChange={onPositionChange}
      onCollapse={onCollapse}
      variant="no-merge"
      extraActions={
        onToggleSecondaryBar && (
          <button
            type="button"
            className={cn(ICON_BUTTON_CLASS, !secondaryBarHidden && "border-brand text-brand")}
            onClick={onToggleSecondaryBar}
            aria-pressed={!secondaryBarHidden}
            aria-label={secondaryBarHidden ? "打开二次推理面板" : "关闭二次推理面板"}
            title={secondaryBarHidden ? "打开二次推理面板" : "关闭二次推理面板"}
            data-testid="selection-toggle-secondary-bar"
          >
            <Icon name="sparkle" size={13} />
          </button>
        )
      }
      minSize={FLOATING_SELECTION_MIN_SIZE}
      maxSize={FLOATING_SELECTION_MAX_SIZE}
      // eslint-disable-next-line no-restricted-syntax -- 展开/收起 morph 比例由当前面板尺寸派生,通过局部 CSS 变量传给 module 动画。
      style={panelMorphStyle}
      headerContent={
        <div className={PANEL_HEADER_CLASS}>
          <span className={PANEL_HEADER_ICON_CLASS} aria-hidden="true">
            <Icon name="tag" size={13} />
          </span>
          <span className={PANEL_HEADER_TEXT_CLASS}>
            <span className={PANEL_HEADER_META_CLASS}>选中对象</span>
            <span className={PANEL_HEADER_TITLE_CLASS}>{title}</span>
          </span>
        </div>
      }
      className={cn(
        SHELL_CLASS,
        styles.expandedPanel,
        panelLeaving && styles.expandedPanelExit,
        linkedToPet && PET_LINKED_SHELL_CLASS,
      )}
    >
      {children}
    </FloatingPanelShell>
  );
}

export function SelectionCardPlaceholder({ summary }: { summary: string }) {
  return (
    <div className={BODY_CLASS}>
      <p className={SUMMARY_CLASS}>{summary}</p>
    </div>
  );
}
