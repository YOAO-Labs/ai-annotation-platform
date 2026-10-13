import type { ReactNode, RefObject } from "react";
import { Dialog as DialogPrimitive } from "radix-ui";

import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import { useElementStyle } from "./useElementStyle";

/**
 * Modal —— Radix Dialog 适配层(v0.17.2)。
 * 保留 `{open,onClose,title?,width?,children}` API(43 处调用零改动);焦点陷阱 / Esc / 滚动锁 /
 * aria-modal 由 Radix 兜底(去掉旧手写 useEffect)。沿用旧布局:可选 header(title + 关闭,带下边框)
 * + 可滚动 body;无 title 则无 header/关闭按钮(与旧一致),补 sr-only Title 满足 Radix a11y。
 * `width` 动态值经 useElementStyle 注入(绕 eslint inline-style)。
 */
interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  width?: number;
  /** Disable for live canvases where backdrop filtering makes compositing expensive. */
  backdropBlur?: boolean;
  /** Mount nested modal layers inside an owning dialog's stacking context. */
  container?: HTMLElement | null;
  /** Ref form for containers that mount after the first render of the modal owner. */
  containerRef?: RefObject<HTMLElement | null>;
  /** Keep Escape scoped to this modal when it is nested in another dialog. */
  stopEscapePropagation?: boolean;
  children: ReactNode;
}

export function Modal({
  open,
  onClose,
  title,
  width = 560,
  backdropBlur = true,
  container,
  containerRef,
  stopEscapePropagation = false,
  children,
}: ModalProps) {
  const contentRef = useElementStyle<HTMLDivElement>({ width });
  const portalContainer = containerRef?.current ?? container ?? undefined;

  return (
    <DialogPrimitive.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogPrimitive.Portal container={portalContainer}>
        <DialogPrimitive.Overlay
          data-modal=""
          data-testid="modal-overlay"
          className={cn(
            "fixed inset-0 z-modal bg-black/40 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0",
            backdropBlur && "backdrop-blur-overlay",
          )}
        />
        <DialogPrimitive.Content
          data-modal=""
          ref={contentRef}
          aria-describedby={undefined}
          onEscapeKeyDown={(event) => {
            if (stopEscapePropagation) event.stopPropagation();
          }}
          onPointerDownOutside={(event) => {
            // Radix can defer outside dismissal until click, after a sibling
            // confirmation has closed. Its action still belongs to that dialog.
            const target = event.detail.originalEvent.target;
            if (
              target instanceof Element &&
              target.closest('[role="dialog"], [role="alertdialog"]')
            ) {
              event.preventDefault();
            }
          }}
          className="fixed left-1/2 top-1/2 z-modal flex max-h-[calc(100vh-48px)] w-full max-w-[calc(100%-48px)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-xl border border-border bg-card shadow-lg outline-none data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95"
        >
          {title !== undefined ? (
            <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
              <DialogPrimitive.Title className="text-sm font-semibold text-foreground">
                {title}
              </DialogPrimitive.Title>
              <DialogPrimitive.Close
                aria-label="关闭"
                className="inline-flex appearance-none items-center rounded border-0 bg-transparent p-1 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Icon name="x" size={16} />
              </DialogPrimitive.Close>
            </div>
          ) : (
            <DialogPrimitive.Title className="sr-only">对话框</DialogPrimitive.Title>
          )}
          <div className="overflow-y-auto px-4 py-4">{children}</div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
