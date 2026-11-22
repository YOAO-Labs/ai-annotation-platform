import { forwardRef } from "react";
import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

import { useElementStyle } from "./useElementStyle";

interface BadgeProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children"> {
  variant?: "default" | "success" | "warning" | "danger" | "accent" | "ai" | "outline";
  dot?: boolean;
  children: ReactNode;
  className?: string;
  style?: React.CSSProperties;
}

const variantClassNames: Record<NonNullable<BadgeProps["variant"]>, string> = {
  default: "bg-muted text-muted-foreground",
  success: "bg-status-positive-soft text-status-positive",
  warning: "bg-status-caution-soft text-status-caution",
  danger: "bg-status-danger-soft text-status-danger",
  accent: "bg-status-info-alt-soft text-status-info-alt",
  ai: "bg-status-info-soft text-status-info",
  outline: "border border-border text-foreground",
};

export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(function Badge(
  { variant = "default", dot, children, className, style, ...spanProps },
  forwardedRef,
) {
  const styleRef = useElementStyle<HTMLSpanElement>(style, forwardedRef);
  return (
    <span
      {...spanProps}
      ref={styleRef}
      className={cn(
        "inline-flex w-fit shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        variantClassNames[variant],
        className,
      )}
    >
      {dot && <span className="size-1.5 rounded-full bg-current opacity-90" />}
      {children}
    </span>
  );
});
