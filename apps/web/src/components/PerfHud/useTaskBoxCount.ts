// 当前 task 框数全局 store, 用于 PerfHud 浏览器侧指标.
// WorkbenchShell 每次渲染时 publish annotations.length; 非工作台路由保留上一次值或 0.

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
