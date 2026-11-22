import { Icon } from "./Icon";
import { cn } from "@/lib/utils";
import { useElementStyle } from "./useElementStyle";

interface SearchInputProps {
  placeholder?: string;
  value?: string;
  onChange?: (value: string) => void;
  width?: number;
  kbd?: string;
  onClick?: () => void;
  readOnly?: boolean;
}

export function SearchInput({
  placeholder = "搜索...",
  value,
  onChange,
  width = 240,
  kbd,
  onClick,
  readOnly,
}: SearchInputProps) {
  const rootRef = useElementStyle<HTMLDivElement>({ width });

  return (
    <div
      ref={rootRef}
      onClick={onClick}
      className={cn(
        "surface-shadow-sm inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 transition-[border-color,box-shadow,transform] duration-200 focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/20",
        onClick && "hover:-translate-y-px hover:border-input hover:bg-accent/60",
        onClick && "cursor-pointer",
      )}
    >
      <Icon name="search" size={13} className="text-muted-foreground" />
      <input
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
        readOnly={readOnly}
        className={cn(
          "min-w-0 flex-1 border-0 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground",
          onClick && "cursor-pointer",
        )}
      />
      {kbd && (
        <span className="rounded border border-b-2 border-border bg-muted px-1.5 py-px font-mono text-2xs leading-none text-muted-foreground">
          {kbd}
        </span>
      )}
    </div>
  );
}
