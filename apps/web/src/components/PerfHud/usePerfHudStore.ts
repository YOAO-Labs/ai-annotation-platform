import { create } from "zustand";

/**
 * PerfHud 浮窗 visibility 全局 store.
 *
 * 触发方式:
 * - Ctrl+Shift+P (workbench hotkeys)
 * - TopBar gear → "性能监控"
 * 权限 gating 由消费组件做 (super_admin / project_admin), store 不关心.
 */
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
