import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

export function ReviewClaimNotice({
  message,
  retry,
}: {
  message: string | null;
  retry?: () => void;
}) {
  if (!message) return null;
  return (
    <div className="flex items-center gap-1.5 border-b border-border bg-status-caution-soft px-3.5 py-1.5 text-xs text-status-caution">
      <Icon name="warning" size={13} />
      <span role="status" className="flex-1">
        {message}
      </span>
      {retry && (
        <Button size="sm" onClick={retry}>
          重试认领
        </Button>
      )}
    </div>
  );
}
