export const SIDE_FLOATING_PANEL_MIN_SIZE = { w: 320, h: 320 } as const;
export const SIDE_FLOATING_PANEL_MAX_SIZE = { w: 720, h: 900 } as const;

// 选中标注浮动信息卡(SelectedAnnotationCard);比边栏浮窗略窄/略矮,
// w/h 上界与后端 FloatingSelectionState 约束(48–720 / 120–900)一致。
export const FLOATING_SELECTION_MIN_SIZE = { w: 300, h: 200 } as const;
export const FLOATING_SELECTION_MAX_SIZE = { w: 560, h: 860 } as const;
