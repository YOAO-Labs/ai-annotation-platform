import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import type { CameraPanelState } from "@/api/auth";
import type { WorkbenchLayoutPatch } from "@/pages/Workbench/state/useWorkbenchConfig";
import { CAMERA_AUTO_COLLAPSE_WIDTH } from "./ThreeDWorkbench.helpers";

interface UseCameraPanelsParams {
  cameraPanels: Record<string, CameraPanelState>;
  onWorkbenchLayoutChange: (patch: WorkbenchLayoutPatch) => void;
  viewportWrapRef: RefObject<HTMLDivElement>;
}

export function useCameraPanels({
  cameraPanels,
  onWorkbenchLayoutChange,
  viewportWrapRef,
}: UseCameraPanelsParams) {
  const [autoCollapseCameras, setAutoCollapseCameras] = useState(false);

  const cameraPanelsRef = useRef(cameraPanels);
  cameraPanelsRef.current = cameraPanels;
  const handleCameraPanelPosition = useCallback(
    (role: string, pos: { x: number; y: number } | null) => {
      const next = { ...cameraPanelsRef.current };
      const prev = next[role];
      if (pos === null) {
        if (prev?.collapsed) {
          next[role] = { x: null, y: null, collapsed: true };
        } else {
          delete next[role];
        }
      } else {
        next[role] = { ...prev, x: pos.x, y: pos.y };
      }
      cameraPanelsRef.current = next;
      onWorkbenchLayoutChange({ cameraPanels: next });
    },
    [onWorkbenchLayoutChange],
  );
  const handleCameraPanelCollapsed = useCallback(
    (role: string, collapsed: boolean) => {
      const prev = cameraPanelsRef.current[role];
      const next = { ...cameraPanelsRef.current };
      if (!collapsed && !autoCollapseCameras && prev?.x == null && prev?.y == null) {
        delete next[role];
      } else {
        next[role] = { x: prev?.x ?? null, y: prev?.y ?? null, collapsed };
      }
      cameraPanelsRef.current = next;
      onWorkbenchLayoutChange({ cameraPanels: next });
    },
    [autoCollapseCameras, onWorkbenchLayoutChange],
  );
  const handleResetCameraPanels = useCallback(() => {
    cameraPanelsRef.current = {};
    onWorkbenchLayoutChange({ cameraPanels: {} });
  }, [onWorkbenchLayoutChange]);

  useLayoutEffect(() => {
    const sync = () => {
      const width = viewportWrapRef.current?.getBoundingClientRect().width ?? window.innerWidth;
      setAutoCollapseCameras(width < CAMERA_AUTO_COLLAPSE_WIDTH);
    };
    sync();
    window.addEventListener("resize", sync);
    if (typeof ResizeObserver === "undefined" || !viewportWrapRef.current) {
      return () => window.removeEventListener("resize", sync);
    }
    const observer = new ResizeObserver(sync);
    observer.observe(viewportWrapRef.current);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", sync);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    autoCollapseCameras,
    handleCameraPanelPosition,
    handleCameraPanelCollapsed,
    handleResetCameraPanels,
  };
}
