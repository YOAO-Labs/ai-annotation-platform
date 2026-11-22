import { cn } from "@/lib/utils";
import { useAvatarPresets } from "@/hooks/useAvatarPresets";
import { presetAvatarUrl } from "@/utils/avatar";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentRef: string | null | undefined;
  pending?: boolean;
  onSelect: (ref: string) => void;
  onClear: () => void;
}

export function AvatarPickerDialog({
  open,
  onOpenChange,
  currentRef,
  pending,
  onSelect,
  onClear,
}: Props) {
  const presets = useAvatarPresets();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[560px]">
        <DialogHeader>
          <DialogTitle>选择内置头像</DialogTitle>
          <DialogDescription>
            像素小人素材来自 DiceBear Pixel Art（CC0 1.0，可自由使用）。选中后立即生效，可随时换回。
          </DialogDescription>
        </DialogHeader>

        {presets.isLoading && (
          <p className="py-6 text-center text-xs text-muted-foreground">头像目录加载中…</p>
        )}

        {presets.isError && (
          <p
            role="alert"
            className="rounded-md border border-status-danger/40 bg-status-danger-soft px-3 py-2 text-xs"
          >
            头像目录加载失败，请刷新后重试。
          </p>
        )}

        {presets.data && (
          <div className="grid max-h-[46vh] grid-cols-8 gap-2 overflow-y-auto py-1 max-[760px]:grid-cols-6">
            {presets.data.items.map((item) => {
              const ref = `preset:${item.id}`;
              const selected = currentRef === ref;
              return (
                <button
                  key={item.id}
                  type="button"
                  title={item.label}
                  aria-label={item.label}
                  aria-pressed={selected}
                  disabled={pending}
                  onClick={() => onSelect(ref)}
                  className={cn(
                    "cursor-pointer rounded-full border border-border bg-muted p-0.5 transition-[box-shadow,border-color] duration-150",
                    "hover:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30 focus-visible:outline-none",
                    selected && "border-ring ring-2 ring-ring/40",
                    pending && "cursor-not-allowed opacity-60",
                  )}
                >
                  <img
                    src={presetAvatarUrl(item.id)}
                    alt=""
                    aria-hidden="true"
                    width={48}
                    height={48}
                    loading="lazy"
                    className="size-full rounded-full"
                  />
                </button>
              );
            })}
          </div>
        )}

        <DialogFooter>
          <button
            type="button"
            className="w-auto cursor-pointer rounded-md border border-border bg-card px-3.5 py-2 text-sm text-foreground disabled:cursor-not-allowed"
            disabled={pending || !currentRef}
            onClick={onClear}
          >
            恢复默认
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
