import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

import { buttonVariants } from "@/components/shadcn/ui/button";
import { cn } from "@/lib/utils";

import { useElementStyle } from "./useElementStyle";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "primary" | "ghost" | "ai" | "danger";
  size?: "xs" | "sm" | "md";
  children: ReactNode;
}

const VARIANT_MAP: Record<
  NonNullable<ButtonProps["variant"]>,
  { base: "default" | "outline" | "ghost"; extra?: string }
> = {
  default: { base: "outline" },
  primary: { base: "default" },
  ghost: { base: "ghost" },
  ai: {
    base: "outline",
    extra: "border-violet-500/30 bg-status-info-soft text-status-info hover:bg-violet-500/15",
  },
  danger: {
    base: "outline",
    extra: "border-rose-500/30 text-status-danger hover:bg-status-danger-soft",
  },
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "default", size = "md", className, children, style: callerStyle, ...props },
  ref,
) {
  const styleRef = useElementStyle(callerStyle, ref);
  const { base, extra } = VARIANT_MAP[variant];
  return (
    <button
      ref={styleRef}
      className={cn(
        "appearance-none border-0 border-border bg-transparent",
        buttonVariants({
          variant: base,
          size: size === "xs" ? "xs" : size === "sm" ? "sm" : "default",
        }),
        "transition-[background-color,border-color,color,box-shadow,transform] duration-200 hover:-translate-y-px active:translate-y-0 active:scale-[0.98]",
        extra,
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
});
