import {
  forwardRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";

import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

export function FilterGroup({
  label = "筛选",
  compact = false,
  hideLabel = false,
  className,
  children,
  ...props
}: HTMLAttributes<HTMLDivElement> & { label?: string; compact?: boolean; hideLabel?: boolean }) {
  return (
    <div
      role="group"
      aria-label={label}
      data-filter-group=""
      className={cn("flex min-w-0 flex-wrap items-center gap-2", compact && "gap-1.5", className)}
      {...props}
    >
      {/* `hideLabel` 用于省去纯装饰的漏斗图标 + 文字，`label` 仍作为 group 的无障碍名称。 */}
      {!hideLabel && (
        <span
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-muted-foreground",
            compact && "text-2xs",
          )}
        >
          <Icon name="filter" className={compact ? "size-3" : "size-4"} />
          {label}
        </span>
      )}
      {children}
    </div>
  );
}

export function FilterSection({
  title,
  children,
  className,
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <fieldset className={cn("min-w-0 space-y-2 border-0 p-0", className)}>
      <legend className="mb-2 text-xs font-medium text-muted-foreground">{title}</legend>
      {children}
    </fieldset>
  );
}

export const FilterToggle = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { active: boolean; compact?: boolean }
>(function FilterToggle({ active, compact = false, className, children, ...props }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      aria-pressed={active}
      data-filter-option=""
      className={cn(
        "inline-flex min-w-0 cursor-pointer appearance-none items-center justify-center gap-1.5 rounded-md border border-transparent bg-transparent px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
        compact && "gap-1 px-2 py-1 text-2xs",
        active && "border-brand/20 bg-brand/10 text-brand hover:bg-brand/10 hover:text-brand",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
});

export const FilterSelect = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement> & { compact?: boolean }
>(function FilterSelect({ compact = false, className, children, ...props }, ref) {
  return (
    <select
      ref={ref}
      data-filter-select=""
      className={cn(
        "h-8 min-w-0 max-w-full cursor-pointer rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground shadow-xs outline-none transition-colors hover:border-ring/40 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30 disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground",
        compact && "h-7 text-xs shadow-none",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
});
