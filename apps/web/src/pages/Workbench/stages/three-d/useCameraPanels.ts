// 第 2 批 · 从 ThreeDWorkbench 抽出的悬浮相机面板布局 hook:
// 相机面板位置/折叠态落库(按 role 分桶,经 onWorkbenchLayoutChange 回写 user config)、
// 窄屏自动折叠(ResizeObserver)。相机位置只读取当前账号偏好。
// viewportWrapRef 由壳层共用(还驱动 toolbar 高度 / triFloatBounds 两个 effect),故作参数传入。
// 逐字搬运,行为零变化。
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

  // 相机面板位置/折叠态落库:多面板可连续拖动,故用 ref 取最新整份 Record,
  // 避免相邻回调读到 props 旧值互相覆盖。复位则删该 role 键。
  const cameraPanelsRef = useRef(cameraPanels);
  cameraPanelsRef.current = cameraPanels;
  const handleCameraPanelPosition = useCallback(
    (role: string, pos: { x: number; y: number } | null) => {
      const next = { ...cameraPanelsRef.current };
      const prev = next[role];
      if (pos === null) {
        // 归位:仅清位置;若仍有非默认折叠态则保留该 role 键,否则整键删除。
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
      // 折叠态独立于位置；宽屏回到默认展开且无自定义位置时整键删除。
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
    // viewportWrapRef 稳定(useRef);仅首挂载装一次 observer。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    autoCollapseCameras,
    handleCameraPanelPosition,
    handleCameraPanelCollapsed,
    handleResetCameraPanels,
  };
}
