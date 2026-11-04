import type { CSSProperties } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

import { useTheme } from "@/hooks/useTheme";

/**
 * sonner Toaster —— 适配本项目主题机制。
 * - 主题不走 shadcn 默认的 next-themes,而是项目自有的 `useTheme().resolved`
 *   (driven by `<html data-theme>`),传入具体的 light/dark,与全站暗色单点对齐。
 * - 颜色用 `--sc-*` token(本项目无裸 `--popover`,只在 `@theme inline` 映射),
 *   保证 toast 与卡片/浮层表面同源。
 */
const Toaster = ({ ...props }: ToasterProps) => {
  const { resolved } = useTheme();

  return (
    <Sonner
      theme={resolved}
      className="toaster group"
      containerAriaLabel="通知"
      // sonner 通过 CSS 变量注入主题色,这是其官方主题 API,无法用 class 表达
      // eslint-disable-next-line no-restricted-syntax
      style={
        {
          "--normal-bg": "var(--sc-popover)",
          "--normal-text": "var(--sc-popover-foreground)",
          "--normal-border": "var(--sc-border)",
        } as CSSProperties
      }
      {...props}
    />
  );
};

export { Toaster };
