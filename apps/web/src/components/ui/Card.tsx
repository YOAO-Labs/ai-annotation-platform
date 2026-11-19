import type { ReactNode, CSSProperties, MouseEvent } from "react";

import { cn } from "@/lib/utils";

import { useElementStyle } from "./useElementStyle";

/**
 * Card —— shadcn / 设计规范适配层。
 * 旧 Card 仅是「表面 + 1px 边框 + 圆角」的容器(**无 padding**),不用 shadcn `<Card>`(其 root 自带
 * `py-6 gap-6 shadow-sm` 会破坏 36 处布局)。这里 1:1 还原:`rounded-xl border bg-card`,无 padding。
 * 新增可选 `className` 透传(旧无)。
 */
interface CardProps {
  children: ReactNode;
  style?: CSSProperties;
  className?: string;
  onClick?: (e: MouseEvent<HTMLDivElement>) => void;
}

export function Card({ children, style, className, onClick }: CardProps) {
  const styleRef = useElementStyle<HTMLDivElement>(style);
  return (
    <div
      ref={styleRef}
      className={cn(
        "surface-shadow-sm rounded-lg border border-border bg-card transition-[border-color,box-shadow,transform] duration-200",
        onClick && "surface-shadow-md-on-hover hover:-translate-y-px",
        className,
      )}
      onClick={onClick}
    >
      {children}
    </div>
  );
}
