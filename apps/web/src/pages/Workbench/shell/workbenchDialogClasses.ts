/**
 * 工作台居中悬浮面板(设置 / 快捷键 / 标注指引)共享的对话框几何与遮罩。
 *
 * 三个入口来自同一类「悬浮大面板」,必须保持同一尺寸、同一遮罩压暗与模糊程度,
 * 否则同类入口的观感会不一致。改这里即三处同步。
 *
 * 遮罩模糊走 `--sc-overlay-blur`(见 styles/shadcn.css)。
 */
export const WORKBENCH_DIALOG_CONTENT_CLASS =
  "z-app-drawer flex h-dvh max-h-dvh w-screen max-w-none flex-col gap-0 overflow-hidden rounded-none border-border bg-card p-0 text-foreground motion-reduce:animate-none sm:max-w-none md:h-[min(820px,85dvh)] md:max-h-[calc(100dvh-64px)] md:w-[min(1120px,calc(100vw-64px))] md:rounded-xl";

/** 遮罩:压暗 + 背景模糊(图片 / 视频工作台)。 */
export const WORKBENCH_DIALOG_OVERLAY_CLASS =
  "z-app-drawer-backdrop bg-black/25 backdrop-blur-overlay motion-reduce:animate-none";

/** 遮罩:只压暗、不模糊(3D / 点云工作台)。 */
export const WORKBENCH_DIALOG_OVERLAY_NO_BLUR_CLASS =
  "z-app-drawer-backdrop bg-black/25 motion-reduce:animate-none";

/**
 * 3D / 点云画布在软件渲染(CI 的 SwiftShader 等)下用 `backdrop-filter` 会拖慢
 * 合成清理,导致后续浏览器上下文初始化超时。这类工作台只压暗、不模糊;
 * 图片 / 视频工作台保持统一的模糊程度。见 Modal 的 `backdropBlur` 同一取舍。
 */
export function workbenchDialogOverlayClass(backdropBlur: boolean): string {
  return backdropBlur ? WORKBENCH_DIALOG_OVERLAY_CLASS : WORKBENCH_DIALOG_OVERLAY_NO_BLUR_CLASS;
}
