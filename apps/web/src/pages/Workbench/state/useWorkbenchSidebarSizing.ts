// 从 useWorkbenchShellModel.tsx 抽出的侧栏像素尺寸计算(自包含连续块)。
// 行为零变化:winWidth 监听 + leftPx/rightPx + onResize* + sidebar*Px 阈值逐字搬运。
import { useCallback, useEffect, useState } from "react";
import { clamp } from "./useWorkbenchShellModel.helpers";
import type { WorkbenchConfigPatch } from "./useWorkbenchConfig";

export interface WorkbenchSidebarSizing {
  leftPx: number;
  rightPx: number;
  onResizeLeft: (px: number) => void;
  onResizeRight: (px: number) => void;
  sidebarMinPx: number;
  sidebarMaxPx: number;
  sidebarResetPx: number;
}

export function useWorkbenchSidebarSizing(
  leftPct: number,
  rightPct: number,
  setWorkbenchFields: (patch: WorkbenchConfigPatch) => void,
): WorkbenchSidebarSizing {
  const [winWidth, setWinWidth] = useState(() =>
    typeof window !== "undefined" ? window.innerWidth : 1440,
  );
  useEffect(() => {
    const onResize = () => setWinWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  // px 供 JS 消费者(AI 面板右偏移等);clamp 与下方栅格 CSS clamp(180px..600px) 对齐。
  const leftPx = Math.round(clamp((leftPct / 100) * winWidth, 180, 600));
  const rightPx = Math.round(clamp((rightPct / 100) * winWidth, 180, 600));
  const onResizeLeft = useCallback(
    (px: number) => {
      const pct = clamp(Math.round((px / winWidth) * 100), 10, 35);
      setWorkbenchFields({ common: { leftWidthPct: pct } });
    },
    [winWidth, setWorkbenchFields],
  );
  const onResizeRight = useCallback(
    (px: number) => {
      const pct = clamp(Math.round((px / winWidth) * 100), 10, 35);
      setWorkbenchFields({ common: { rightWidthPct: pct } });
    },
    [winWidth, setWorkbenchFields],
  );
  // 拖拽/双击重置共用的 px 边界:10%..35% 换成像素,resetTo 为 15% 像素值(回换正好落 15%)。
  const sidebarMinPx = Math.round(0.1 * winWidth);
  const sidebarMaxPx = Math.round(0.35 * winWidth);
  const sidebarResetPx = Math.round(0.15 * winWidth);
  return {
    leftPx,
    rightPx,
    onResizeLeft,
    onResizeRight,
    sidebarMinPx,
    sidebarMaxPx,
    sidebarResetPx,
  };
}
