import type { ReactNode } from "react";

import { copyToClipboard } from "@/lib/clipboard";
import { Button } from "./Button";
import { Icon } from "./Icon";
import { useToastStore } from "./Toast";

interface CopyButtonProps {
  value: string;
  label: string;
  children?: ReactNode;
  className?: string;
  title?: string;
  successMessage?: string;
  disabled?: boolean;
}

export function CopyButton({
  value,
  label,
  children,
  className,
  title = label,
  successMessage = "已复制",
  disabled,
}: CopyButtonProps) {
  const pushToast = useToastStore((state) => state.push);
  return (
    <Button
      type="button"
      variant="ghost"
      size="xs"
      aria-label={label}
      title={title}
      className={className}
      disabled={disabled}
      onPointerDown={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
      onKeyUp={(event) => event.stopPropagation()}
      onClick={async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const copied = await copyToClipboard(value);
        pushToast({
          msg: copied ? successMessage : "复制失败，请检查浏览器剪贴板权限",
          kind: copied ? "success" : "error",
        });
      }}
    >
      {children}
      <Icon name="copy" size={12} className="shrink-0 text-muted-foreground" />
    </Button>
  );
}
