// WS 重连计数全局 store, 用于 PerfHud 浏览器侧指标.
// useReconnectingWebSocket 每触发一次 scheduleReconnect 即 bump 一次.
// 计数是全局累计 (跨 unmount); 24h 后由 PerfHud 显示侧自行展示 "since session start".

import { create } from "zustand";

interface WsMetricsState {
  reconnects: number;
  bump: () => void;
  reset: () => void;
}

export const useWsMetricsStore = create<WsMetricsState>((set) => ({
  reconnects: 0,
  bump: () => set((s) => ({ reconnects: s.reconnects + 1 })),
  reset: () => set({ reconnects: 0 }),
}));

/** 在 hook 外调用(useReconnectingWebSocket 的 scheduleReconnect 里). */
export function bumpWsReconnectCount(): void {
  useWsMetricsStore.getState().bump();
}
