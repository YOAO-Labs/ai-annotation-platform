import type { ReactElement, ReactNode } from "react";

import {
  Tooltip as ShadcnTooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/shadcn/ui/tooltip";

type Side = "right" | "left" | "top" | "bottom";

interface TooltipProps {
  name: ReactNode;
  desc?: ReactNode;
  hotkey?: string;
  side?: Side;
  delay?: number;
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
