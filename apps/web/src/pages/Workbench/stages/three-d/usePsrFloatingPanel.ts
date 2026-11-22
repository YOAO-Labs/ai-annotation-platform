import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  readPsrPanelUiState,
  writePsrPanelUiState,
  type PsrPanelUiState,
} from "./pointcloudPreferenceStorage";
import type { FloatingPanelPoint } from "../../shell/useDragMove";

interface LinkedPsrPanelAnchor {
  position: FloatingPanelPoint;
  onPositionChange: (position: FloatingPanelPoint) => void;
}

type PsrDragState =
  | { mode: "panel"; sx: number; sy: number; dx0: number; dy0: number }
  | { mode: "linked"; sx: number; sy: number; x0: number; y0: number };

export function usePsrFloatingPanel(
  userId: string | null,
  linkedAnchor: LinkedPsrPanelAnchor | null = null,
) {
  const [psrPanel, setPsrPanel] = useState<PsrPanelUiState>({ expanded: false, dx: 0, dy: 0 });
  const psrPanelRef = useRef(psrPanel);
  psrPanelRef.current = psrPanel;
  const linkedAnchorRef = useRef(linkedAnchor);
  linkedAnchorRef.current = linkedAnchor;
  useEffect(() => {
    if (!userId || typeof window === "undefined") return;
    setPsrPanel(readPsrPanelUiState(userId, window.localStorage));
  }, [userId]);
  const persistPsrPanel = useCallback(
    (next: PsrPanelUiState) => {
      if (userId && typeof window !== "undefined")
        writePsrPanelUiState(userId, next, window.localStorage);
    },
    [userId],
  );
  const togglePsrExpanded = useCallback(() => {
    setPsrPanel((p) => {
      const next = { ...p, expanded: !p.expanded };
      persistPsrPanel(next);
      return next;
    });
  }, [persistPsrPanel]);
  const psrDragRef = useRef<PsrDragState | null>(null);
  const [psrDragging, setPsrDragging] = useState(false);
  const onPsrHeaderPointerDown = useCallback((e: ReactPointerEvent) => {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button, select, input, a")) return;
    const anchor = linkedAnchorRef.current;
    psrDragRef.current = anchor
      ? {
          mode: "linked",
          sx: e.clientX,
          sy: e.clientY,
          x0: anchor.position.x,
          y0: anchor.position.y,
        }
      : {
          mode: "panel",
          sx: e.clientX,
          sy: e.clientY,
          dx0: psrPanelRef.current.dx,
          dy0: psrPanelRef.current.dy,
        };
    setPsrDragging(true);
  }, []);
  useEffect(() => {
    if (!psrDragging) return;
    const onMove = (e: PointerEvent) => {
      const d = psrDragRef.current;
      if (!d) return;
      if (d.mode === "linked") {
        linkedAnchorRef.current?.onPositionChange({
          x: d.x0 + e.clientX - d.sx,
          y: d.y0 + e.clientY - d.sy,
        });
        return;
      }
      setPsrPanel((p) => ({
        ...p,
        dx: d.dx0 + e.clientX - d.sx,
        dy: d.dy0 + e.clientY - d.sy,
      }));
    };
    const onUp = () => {
      const d = psrDragRef.current;
      psrDragRef.current = null;
      setPsrDragging(false);
      if (d?.mode === "panel") persistPsrPanel(psrPanelRef.current);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [psrDragging, persistPsrPanel]);

  return { psrPanel, psrDragging, onPsrHeaderPointerDown, togglePsrExpanded };
}
