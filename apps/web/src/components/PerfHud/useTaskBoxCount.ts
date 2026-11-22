import { create } from "zustand";

interface TaskBoxCountState {
  count: number;
  set: (n: number) => void;
}

export const useTaskBoxCountStore = create<TaskBoxCountState>((set) => ({
  count: 0,
  set: (n) => set({ count: n }),
}));

export function publishTaskBoxCount(n: number): void {
  useTaskBoxCountStore.getState().set(n);
}
