import { useEffect, useRef } from "react";
import { create } from "zustand";

interface FabRevealStore {
  revealed: boolean;
  set: (v: boolean) => void;
}

export const useFabRevealStore = create<FabRevealStore>((set) => ({
  revealed: false,
  set: (v) => set({ revealed: v }),
}));

export const useFabRevealed = (): boolean => useFabRevealStore((s) => s.revealed);

const ZONE_W = 64;
const ZONE_H = 48;
const HIDE_DELAY_MS = 450;

export function isPointerInFabRevealZone(
  clientX: number,
  clientY: number,
  viewportWidth: number,
  viewportHeight: number,
): boolean {
  return clientX >= viewportWidth - ZONE_W && clientY >= viewportHeight - ZONE_H;
}

export function isPointerOverFabTarget(target: EventTarget | null): boolean {
  return (
    target instanceof Element && target.closest("[data-bug-fab], [data-workbench-fab]") !== null
  );
}

export function useFabAutoHideDriver(): void {
  const hideTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const inZone =
        isPointerInFabRevealZone(e.clientX, e.clientY, window.innerWidth, window.innerHeight) ||
        isPointerOverFabTarget(e.target);
      const { revealed, set } = useFabRevealStore.getState();
      if (inZone) {
        if (hideTimer.current !== undefined) {
          window.clearTimeout(hideTimer.current);
          hideTimer.current = undefined;
        }
        if (!revealed) set(true);
      } else if (revealed && hideTimer.current === undefined) {
        hideTimer.current = window.setTimeout(() => {
          useFabRevealStore.getState().set(false);
          hideTimer.current = undefined;
        }, HIDE_DELAY_MS);
      }
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (hideTimer.current !== undefined) window.clearTimeout(hideTimer.current);
    };
  }, []);
}
