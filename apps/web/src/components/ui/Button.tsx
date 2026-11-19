import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

import { buttonVariants } from "@/components/shadcn/ui/button";
import { cn } from "@/lib/utils";

import { useElementStyle } from "./useElementStyle";

/**
 * Button —— shadcn 适配层。
 * 保留原有 `variant/size` API(调用点零改动),内部改走 shadcn 的 `buttonVariants`。
 * 渲染原生 `<button>` 以保留 forwardRef + useElementStyle(`style` 透传,绕 eslint inline-style 禁令)。
 * 映射见 docs/plans/archive/2026-06-19-v0.17.1-ui-primitives-wave1.md §3.1。
 */
interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "primary" | "ghost" | "ai" | "danger";
  // 补 xs (shadcn 既有档): h-6 + text-xs + svg 12px, 比 sm 更紧凑。
  // 注意 shadcn 基类用 `[&_svg]:size-4` 以 CSS 锁定按钮内图标 16px, 会覆盖 <Icon size>,
  // 故按钮想要更小图标须经 size 档 (xs→size-3), 改 Icon size 属性无效。
  size?: "xs" | "sm" | "md";
  children: ReactNode;
}

// 旧 variant → shadcn variant + 额外语义点缀 className(设计 §2.2)
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
        // 迁移期未启用全局 Tailwind preflight(只在 .内重置),而本适配器渲染的原生
        // 避免原生 <button> 透出浏览器默认的灰底和粗边。
        // 显式使用 appearance-none、透明底和零宽边框；
        // 各 variant 用 twMerge 覆盖 (outline 的 border 覆盖宽度，bg-* 覆盖底色)。
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
