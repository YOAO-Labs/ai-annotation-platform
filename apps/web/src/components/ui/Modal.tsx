import type { ReactNode, RefObject } from "react";
import { Dialog as DialogPrimitive } from "radix-ui";

import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import { useElementStyle } from "./useElementStyle";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  width?: number;
  backdropBlur?: boolean;
  container?: HTMLElement | null;
  containerRef?: RefObject<HTMLElement | null>;
  stopEscapePropagation?: boolean;
  returnFocusRef?: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  footer?: ReactNode;
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
  returnFocusRef,
  initialFocusRef,
  footer,
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
          onOpenAutoFocus={(event) => {
            const target = initialFocusRef?.current;
            if (target?.isConnected) {
              event.preventDefault();
              target.focus();
            }
          }}
          onCloseAutoFocus={(event) => {
            const target = returnFocusRef?.current;
            if (target?.isConnected) {
              event.preventDefault();
              target.focus();
            }
          }}
          onEscapeKeyDown={(event) => {
            if (stopEscapePropagation) event.stopPropagation();
          }}
          onPointerDownOutside={(event) => {
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
            <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3.5">
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
          <div className="min-h-0 overflow-y-auto px-4 py-4">{children}</div>
          {footer != null && (
            <div className="shrink-0 border-t border-border bg-card px-4 py-3">{footer}</div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
