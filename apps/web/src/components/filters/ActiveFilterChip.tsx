import { forwardRef, type ButtonHTMLAttributes } from "react";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

type Props = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
  label: string;
  value?: string;
  invalid?: boolean;
  onRemove?: () => void;
};

export const ActiveFilterChip = forwardRef<HTMLButtonElement, Props>(function ActiveFilterChip(
  { label, value, invalid, onRemove, className, onClick, ...props },
  ref,
) {
  const text = `${value ? `${label}：${value}` : label}${invalid ? "（条件无效）" : ""}`;
  const content = (
    <>
      <span className="truncate">{label}</span>
      {value && <span className="truncate text-muted-foreground">：{value}</span>}
      {invalid && <span className="ml-1 text-status-danger">无效</span>}
    </>
  );
  const mainClass = "inline-flex min-w-0 items-center px-2 py-1 text-xs";
  return (
    <span
      data-filter-chip=""
      data-invalid={invalid || undefined}
      className={cn(
        "inline-flex max-w-64 items-center overflow-hidden rounded-md border border-border bg-muted/40 text-foreground",
        invalid && "border-status-danger/40",
        className,
      )}
    >
      {onClick || props["aria-haspopup"] ? (
        <button
          ref={ref}
          type="button"
          aria-label={text}
          title={text}
          className={cn(
            mainClass,
            "cursor-pointer bg-transparent hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
          )}
          onClick={onClick}
          {...props}
        >
          {content}
        </button>
      ) : (
        <span className={mainClass} title={text}>
          {content}
        </span>
      )}
      {onRemove && (
        <button
          type="button"
          aria-label={`移除${label}筛选`}
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
          className="inline-flex shrink-0 cursor-pointer items-center self-stretch border-l border-border bg-transparent px-1 text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <Icon name="x" className="size-3" />
        </button>
      )}
    </span>
  );
});
