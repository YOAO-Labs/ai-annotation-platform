import { useId, type ReactElement, type ReactNode } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/shadcn/ui/popover";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from "@/components/shadcn/ui/sheet";
import { Icon } from "@/components/ui/Icon";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { cn } from "@/lib/utils";

type FilterPanelProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger: ReactElement;
  title?: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  align?: "start" | "center" | "end";
  responsive?: boolean;
  className?: string;
};

export function FilterPanel({
  open,
  onOpenChange,
  trigger,
  title = "筛选",
  description,
  children,
  footer,
  align = "end",
  responsive = true,
  className,
}: FilterPanelProps) {
  const narrow = useMediaQuery("(max-width: 639px)");
  const mobile = responsive && narrow;
  const titleId = useId();
  const descriptionId = useId();
  const header = (
    <div className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3">
      <div className="min-w-0 space-y-1">
        {mobile ? (
          <SheetTitle id={titleId} className="text-sm">
            {title}
          </SheetTitle>
        ) : (
          <h2 id={titleId} className="text-sm font-semibold">
            {title}
          </h2>
        )}
        {description &&
          (mobile ? (
            <SheetDescription id={descriptionId} className="text-xs">
              {description}
            </SheetDescription>
          ) : (
            <p id={descriptionId} className="text-xs text-muted-foreground">
              {description}
            </p>
          ))}
      </div>
      <button
        type="button"
        aria-label="关闭筛选"
        onClick={() => onOpenChange(false)}
        className="inline-flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Icon name="x" className="size-4" />
      </button>
    </div>
  );
  const body = (
    <>
      <div className="min-h-0 overflow-y-auto overscroll-contain px-4 py-3">{children}</div>
      {footer && (
        <div className="shrink-0 border-t border-border bg-muted/20 px-4 py-3">{footer}</div>
      )}
    </>
  );

  if (mobile)
    return (
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetTrigger asChild>{trigger}</SheetTrigger>
        <SheetContent
          side="bottom"
          showCloseButton={false}
          aria-labelledby={titleId}
          aria-describedby={description ? descriptionId : undefined}
          data-filter-panel="sheet"
          className="max-h-[85dvh] gap-0 overflow-hidden rounded-t-xl border-border bg-popover p-0 pb-[env(safe-area-inset-bottom)] text-popover-foreground data-[state=open]:duration-150 data-[state=closed]:duration-150 motion-reduce:animate-none"
        >
          {header}
          {body}
        </SheetContent>
      </Sheet>
    );

  return (
    <Popover open={open} onOpenChange={onOpenChange} modal={false}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        side="bottom"
        align={align}
        sideOffset={8}
        collisionPadding={12}
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        data-filter-panel="popover"
        className={cn(
          "flex max-h-[min(36rem,var(--radix-popover-content-available-height))] w-96 max-w-[calc(100vw-24px)] flex-col gap-0 overflow-hidden rounded-xl border-border p-0 shadow-lg motion-reduce:animate-none",
          className,
        )}
      >
        {header}
        {body}
      </PopoverContent>
    </Popover>
  );
}
