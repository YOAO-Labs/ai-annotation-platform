import { copyToClipboard } from "@/lib/clipboard";

import { Button } from "./Button";
import { Icon } from "./Icon";
import { useToastStore } from "./Toast";
import { Tooltip } from "./Tooltip";

interface CopyValueButtonProps {
  value?: string | null;
  kind?: "hash" | "id";
  label?: string;
}

export function CopyValueButton({ value, kind = "hash", label }: CopyValueButtonProps) {
  const pushToast = useToastStore((state) => state.push);
  const action = kind === "hash" ? "复制哈希" : "复制 ID";

  if (!value) return <span className="text-muted-foreground">—</span>;

  return (
    <Tooltip name={action} side="top">
      <Button
        type="button"
        size="xs"
        variant="ghost"
        className="size-6 shrink-0 p-0 align-middle text-muted-foreground hover:text-foreground pointer-coarse:size-11"
        aria-label={label ?? action}
        onPointerDown={(event) => event.stopPropagation()}
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
        onKeyUp={(event) => event.stopPropagation()}
        onClick={async (event) => {
          event.preventDefault();
          event.stopPropagation();
          const copied = await copyToClipboard(value);
          pushToast({
            msg: copied ? `已${action}` : "复制失败，请检查浏览器剪贴板权限",
            kind: copied ? "success" : "error",
          });
        }}
      >
        <Icon name="copy" size={12} />
      </Button>
    </Tooltip>
  );
}
