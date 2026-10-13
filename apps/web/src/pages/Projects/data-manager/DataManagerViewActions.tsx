import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

const actionClassName =
  "h-7 gap-1.5 px-2 font-normal shadow-none has-[>svg]:px-2 hover:translate-y-0 active:scale-100";

export function DataManagerViewActions({
  analyticsOpen,
  onToggleAnalytics,
  onRefresh,
  refreshDisabled,
  onSave,
  saveDisabled,
}: {
  analyticsOpen: boolean;
  onToggleAnalytics: () => void;
  onRefresh: () => void;
  refreshDisabled: boolean;
  onSave: () => void;
  saveDisabled: boolean;
}) {
  return (
    <div
      role="group"
      aria-label="视图操作"
      className="flex shrink-0 items-center justify-end gap-1.5"
    >
      <Button
        size="xs"
        className={cn(
          actionClassName,
          analyticsOpen && "bg-accent text-accent-foreground dark:bg-accent",
        )}
        aria-pressed={analyticsOpen}
        onClick={onToggleAnalytics}
      >
        <Icon name="chartAnalytics" size={12} />
        统计
      </Button>
      <Button size="xs" className={actionClassName} onClick={onRefresh} disabled={refreshDisabled}>
        <Icon name="refresh" size={12} />
        刷新
      </Button>
      <Button size="xs" className={actionClassName} onClick={onSave} disabled={saveDisabled}>
        <Icon name="save" size={12} />
        保存视图
      </Button>
    </div>
  );
}
