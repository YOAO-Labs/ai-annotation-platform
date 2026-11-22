import type { ReactNode, CSSProperties, MouseEvent } from "react";

import { cn } from "@/lib/utils";

import { useElementStyle } from "./useElementStyle";

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
