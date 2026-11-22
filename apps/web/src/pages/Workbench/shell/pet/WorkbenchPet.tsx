import { useCallback, useRef, useState, type CSSProperties } from "react";
import { cn } from "@/lib/utils";
import { useDragMove, type FloatingPanelPoint } from "../useDragMove";
import { DEFAULT_PET_SKIN } from "./petSkins";
import { usePetState, type WorkbenchPetContext } from "./usePetState";
import styles from "./pet.module.css";

export interface WorkbenchPetProps {
  context: WorkbenchPetContext;
  position?: FloatingPanelPoint;
  onPositionChange?: (position: FloatingPanelPoint) => void;
  detailsVisible?: boolean;
  onExpand: () => void;
}

export const WORKBENCH_PET_SIZE = { w: 56, h: 56 } as const;

export interface WorkbenchPetDock {
  enabled: boolean;
  position: FloatingPanelPoint;
  onPositionChange: (position: FloatingPanelPoint) => void;
  onDetailsVisibleChange?: (visible: boolean) => void;
}

const PET_POS_KEY = "workbench.pet.pos";
const DRAG_THRESHOLD = 3;

export function readWorkbenchPetPosition(): FloatingPanelPoint {
  if (typeof window === "undefined") return { x: 0, y: 0 };
  try {
    const raw = window.localStorage.getItem(PET_POS_KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<FloatingPanelPoint>;
      if (typeof p.x === "number" && typeof p.y === "number") return { x: p.x, y: p.y };
    }
  } catch {
    /* best-effort */
  }
  return { x: window.innerWidth - 230, y: window.innerHeight - 150 };
}

export function writeWorkbenchPetPosition(position: FloatingPanelPoint): void {
  try {
    window.localStorage.setItem(PET_POS_KEY, JSON.stringify(position));
  } catch {
    /* best-effort */
  }
}

export function WorkbenchPet({
  context,
  position: controlledPosition,
  onPositionChange,
  detailsVisible = false,
  onExpand,
}: WorkbenchPetProps) {
  const [poke, setPoke] = useState(0);
  const [uncontrolledPosition, setUncontrolledPosition] =
    useState<FloatingPanelPoint>(readWorkbenchPetPosition);
  const movedRef = useRef(false);
  const startRef = useRef<FloatingPanelPoint | null>(null);
  const { mood, message } = usePetState({ context, poke });
  const skin = DEFAULT_PET_SKIN;
  const position = controlledPosition ?? uncontrolledPosition;
  const canExpand = !detailsVisible && context.selection.count > 0 && context.selection.collapsed;

  const onChange = useCallback(
    (pos: FloatingPanelPoint) => {
      const start = startRef.current;
      if (
        start &&
        (Math.abs(pos.x - start.x) > DRAG_THRESHOLD || Math.abs(pos.y - start.y) > DRAG_THRESHOLD)
      ) {
        movedRef.current = true;
      }
      if (!controlledPosition) setUncontrolledPosition(pos);
      writeWorkbenchPetPosition(pos);
      onPositionChange?.(pos);
    },
    [controlledPosition, onPositionChange],
  );

  const drag = useDragMove({
    position,
    size: WORKBENCH_PET_SIZE,
    onStart: (pos) => {
      startRef.current = pos;
      movedRef.current = false;
    },
    onChange,
  });

  const bubbleText = detailsVisible ? null : message;

  const act = () => {
    if (canExpand) onExpand();
    else setPoke((n) => n + 1);
  };

  return (
    <div
      data-floating-panel
      data-pet-mood={mood}
      tabIndex={0}
      aria-label={
        canExpand ? `展开选中信息卡:${context.selection.title ?? ""}(可拖动)` : "工作台桌宠(可拖动)"
      }
      className={cn(
        "fixed left-[var(--pet-x)] top-[var(--pet-y)] z-popover-elevated flex size-14 cursor-grab touch-none select-none items-center justify-center",
        drag.isDragging && "cursor-grabbing",
      )}
      // eslint-disable-next-line no-restricted-syntax -- 拖动位置经 CSS 变量注入(对齐选中卡折叠 tab 的做法)。
      style={{ "--pet-x": `${position.x}px`, "--pet-y": `${position.y}px` } as CSSProperties}
      {...drag.handleProps}
      onClick={() => {
        if (!movedRef.current) act();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          act();
        }
      }}
    >
      {bubbleText && (
        <div
          className={cn(
            "absolute bottom-full left-1/2 mb-1 w-max max-w-[200px] -translate-x-1/2 border bg-card px-2.5 py-1.5 text-xs text-foreground shadow-md",
            canExpand ? "rounded-md border-brand" : "rounded-lg border-border",
            mood === "warning" &&
              "border-status-caution bg-status-caution-soft text-status-caution",
            mood === "offline" && "border-status-danger bg-status-danger-soft text-status-danger",
            mood === "aiRunning" && "border-brand bg-status-info-soft text-status-info",
            mood === "candidateReady" && "border-brand bg-status-info-soft text-status-info",
            styles.bubble,
          )}
        >
          <span className="block overflow-hidden text-ellipsis whitespace-nowrap font-medium">
            {bubbleText}
          </span>
          {canExpand && <span className="text-muted-foreground">▸ 点我展开</span>}
        </div>
      )}
      <span className={styles.bob}>{skin.renderSprite({ mood })}</span>
    </div>
  );
}
