// 工作台顶部居中悬浮工具条的共享外观 chrome。
//
// 抽自 InteractiveToolBar（SAM 交互工具条），供它与视频 tracker 传播工具条
// (VideoTrackerPropagateDialog) 共用同一套壳/字段标签/下拉/分隔符风格 —— 让两者
// 「看上去是同一族浮动工具条」。仅样式常量与静态分隔符，无状态、无逻辑。
//
// 定位/层级由各自决定（InteractiveToolBar 贴 stage 内 absolute·top-3·z-local-5；
// tracker 工具条 fixed·top-16·z-workbench-modal 以让出底部时间轴），这里只给「内部
// chrome」（圆角/边框/底色/阴影/纵向 flex 间距/内边距）。

// 字段标签：whitespace-nowrap + shrink-0，避免被 flex 挤压逐字竖排，面板按内容自适应加宽。
export const TOOLBAR_FIELD_LABEL_CLASS =
  "shrink-0 whitespace-nowrap text-2xs text-muted-foreground";

// 内联紧凑下拉（引擎/模型/范围/尺寸等），与工具条整体风格统一。
export const TOOLBAR_SELECT_CLASS =
  "appearance-none rounded-sm border border-border bg-muted px-1.5 py-1 text-xs text-foreground";

// 悬浮工具条的共享表面；横排 / 纵排结构由具体工具条决定。
export const TOOLBAR_SURFACE_CLASS =
  "rounded-md border border-border bg-card px-3 py-1.5 shadow-md";

// 悬浮工具条的内部 chrome（不含定位/层级，由各组件在外层补 absolute/fixed + top + z + translate）。
export const TOOLBAR_CHROME_CLASS = `flex flex-col gap-1 ${TOOLBAR_SURFACE_CLASS}`;

// 竖向细分隔符（同一静态元素可在多处复用）。
export const TOOLBAR_DIVIDER = <span aria-hidden className="h-5 w-px bg-border" />;
