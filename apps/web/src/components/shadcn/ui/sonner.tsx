import type { CSSProperties } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

import { useTheme } from "@/hooks/useTheme";

const Toaster = ({ ...props }: ToasterProps) => {
  const { resolved } = useTheme();

  return (
    <Sonner
      theme={resolved}
      className="toaster group"
      containerAriaLabel="通知"
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
