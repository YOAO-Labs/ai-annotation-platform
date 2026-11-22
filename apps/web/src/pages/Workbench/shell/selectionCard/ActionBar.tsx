import type { ReactNode } from "react";

const ACTION_BAR_CLASS =
  "sticky bottom-0 z-local-1 -mx-3 mt-auto flex flex-wrap items-center justify-center gap-1.5 border-t border-border bg-card px-3 pt-2 pb-2.5";

export interface ActionBarProps {
  children: ReactNode;
  label?: string;
}

export function ActionBar({ children, label }: ActionBarProps) {
  return (
    <div className={ACTION_BAR_CLASS} role="group" aria-label={label} data-floating-panel-no-drag>
      {children}
    </div>
  );
}
