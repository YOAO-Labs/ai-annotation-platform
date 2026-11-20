import type { ReactNode } from "react";

const ACTION_BAR_CLASS =
  "sticky bottom-0 z-local-1 -mx-3 mt-auto flex flex-wrap items-center justify-center gap-1.5 border-t border-border bg-card px-3 pt-2 pb-2.5";

export interface ActionBarProps {
  children: ReactNode;
  /** 无障碍分组名(如「标注操作」「预测操作」)。 */
  label?: string;
}

/**
 * 选中信息卡底部动作组容器。各端动作不同(图片=改类/隐藏/锁定/删除;
 * AI=采纳/精修/忽略;视频单帧=跳帧/改类/删除),共用同一容器以统一卡内动作区视觉。
 * 标 data-floating-panel-no-drag,点击按钮不触发卡片拖动。
 */
export function ActionBar({ children, label }: ActionBarProps) {
  return (
    <div className={ACTION_BAR_CLASS} role="group" aria-label={label} data-floating-panel-no-drag>
      {children}
    </div>
  );
}
