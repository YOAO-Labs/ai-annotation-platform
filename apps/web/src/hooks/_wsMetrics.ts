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

export function bumpWsReconnectCount(): void {
  useWsMetricsStore.getState().bump();
}
