import { cn } from "@/lib/utils";
import { CopyButton } from "./CopyButton";

interface CopyableErrorProps {
  message: string | null | undefined;
  /** A short visible summary; copying always uses the complete message. */
  preview?: string;
  className?: string;
  truncate?: boolean;
  label?: string;
  iconOnly?: boolean;
}

export function CopyableError({
  message,
  preview,
  className,
  truncate = false,
  label = "复制完整错误信息",
  iconOnly = false,
}: CopyableErrorProps) {
  if (!message?.trim()) return <span className={className}>—</span>;

  return (
    <CopyButton
      value={message}
      label={label}
      successMessage="已复制完整错误信息"
      title={`${message}\n\n点击复制完整错误信息`}
      className={cn(
        "inline-flex h-auto min-w-0 max-w-full items-start justify-start gap-1.5 whitespace-normal rounded-sm p-0 text-left text-inherit [font:inherit] hover:translate-y-0 active:scale-100 [&_svg]:mt-0.5",
        iconOnly && "p-1",
        className,
      )}
    >
      {!iconOnly && (
        <span
          className={cn(
            "min-w-0",
            truncate ? "truncate" : "max-h-40 overflow-y-auto whitespace-pre-wrap break-words",
          )}
        >
          {preview ?? message}
        </span>
      )}
    </CopyButton>
  );
}
