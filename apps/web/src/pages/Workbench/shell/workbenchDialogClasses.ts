export const WORKBENCH_DIALOG_CONTENT_CLASS =
  "z-app-drawer flex h-dvh max-h-dvh w-screen max-w-none flex-col gap-0 overflow-hidden rounded-none border-border bg-card p-0 text-foreground motion-reduce:animate-none sm:max-w-none md:h-[min(820px,85dvh)] md:max-h-[calc(100dvh-64px)] md:w-[min(1120px,calc(100vw-64px))] md:rounded-xl";

export const WORKBENCH_DIALOG_OVERLAY_CLASS =
  "z-app-drawer-backdrop bg-black/25 backdrop-blur-overlay motion-reduce:animate-none";

export const WORKBENCH_DIALOG_OVERLAY_NO_BLUR_CLASS =
  "z-app-drawer-backdrop bg-black/25 motion-reduce:animate-none";

export function workbenchDialogOverlayClass(backdropBlur: boolean): string {
  return backdropBlur ? WORKBENCH_DIALOG_OVERLAY_CLASS : WORKBENCH_DIALOG_OVERLAY_NO_BLUR_CLASS;
}
