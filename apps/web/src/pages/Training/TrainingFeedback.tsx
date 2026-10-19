import { CopyableError } from "@/components/ui/CopyableError";
import { AlertCircle } from "lucide-react";

export function TrainingLoading({ label, rows = 3 }: { label: string; rows?: number }) {
  return (
    <div role="status" className="p-5">
      <span className="sr-only">{label}</span>
      <div aria-hidden="true" className="space-y-5 motion-safe:animate-pulse">
        {Array.from({ length: rows }, (_, index) => (
          <div key={index} className="flex items-center gap-4">
            <div className="size-9 shrink-0 rounded-md bg-muted" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-2/5 rounded bg-muted" />
              <div className="h-2 w-3/5 rounded bg-muted" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function TrainingError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2.5 rounded-lg bg-status-danger-soft p-4 text-xs text-status-danger"
    >
      <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <p className="min-w-0 flex-1 break-words leading-relaxed">
        <CopyableError message={message} />
      </p>
      <button
        className="shrink-0 rounded-sm underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={onRetry}
      >
        重试
      </button>
    </div>
  );
}
