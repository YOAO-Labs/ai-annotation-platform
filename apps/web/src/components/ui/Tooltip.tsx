import type { ReactElement, ReactNode } from "react";

import {
  Tooltip as ShadcnTooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/shadcn/ui/tooltip";

/**
 * Tooltip —— shadcn(Radix)适配层。
 * 保留 `{name,desc?,hotkey?,side?,delay?,children}` API(调用点零改动);定位/hover/focus/Esc
 * 由 Radix 兜底,去掉手写 portal + 定位。Provider 内置(每用例自带,免改 App 根)。
 */
type Side = "right" | "left" | "top" | "bottom";

interface TooltipProps {
  /** 主标题(粗体首行)。 */
  name: ReactNode;
  /** 描述(次行,灰)。 */
  desc?: ReactNode;
  /** hotkey 徽(kbd 样式末行;多键用空格分隔,如 "Ctrl Z")。 */
  hotkey?: string;
  /** 显示位置(默认 right)。 */
  side?: Side;
  /** hover 触发延迟 ms(默认 200)。 */
  delay?: number;
  /** 子元素必须是单个 ReactElement(如 <button>),Radix 通过 asChild 附加 ref + 事件。 */
  children: ReactElement;
}

export function Tooltip({
  name,
  desc,
  hotkey,
  side = "right",
  delay = 200,
  children,
}: TooltipProps) {
  return (
    <TooltipProvider delayDuration={delay}>
      <ShadcnTooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side={side}>
          <div className="font-medium">{name}</div>
          {desc && <div className="text-muted-foreground">{desc}</div>}
          {hotkey && (
            <div className="mt-1 flex gap-1">
              {hotkey.split(/\s+/).map((k, i) => (
                <kbd
                  key={i}
                  className="rounded border border-border bg-muted px-1 text-2xs leading-tight text-muted-foreground"
                >
                  {k}
                </kbd>
              ))}
            </div>
          )}
        </TooltipContent>
      </ShadcnTooltip>
    </TooltipProvider>
  );
}
