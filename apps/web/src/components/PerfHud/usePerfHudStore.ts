import { create } from "zustand";

interface PerfHudStore {
  visible: boolean;
  expanded: boolean;
  open: () => void;
  close: () => void;
  toggle: () => void;
  setExpanded: (v: boolean) => void;
}

export const usePerfHudStore = create<PerfHudStore>((set) => ({
  visible: false,
  expanded: false,
  open: () => set({ visible: true }),
  close: () => set({ visible: false }),
  toggle: () => set((s) => ({ visible: !s.visible })),
  setExpanded: (v) => set({ expanded: v }),
}));
