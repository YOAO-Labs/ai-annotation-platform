import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

interface Props {
  stroke: string;
  onSetStroke: (color: string) => void;
  shapeCount: number;
  onUndo: () => void;
  onClear: () => void;
  onCancel: () => void;
  onDone: () => void;
}

const SWATCHES = [
  { value: "#ef4444", label: "红", bg: "bg-red-500" },
  { value: "#f59e0b", label: "黄", bg: "bg-amber-500" },
  { value: "#10b981", label: "绿", bg: "bg-emerald-500" },
  { value: "#3b82f6", label: "蓝", bg: "bg-blue-500" },
];

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export function CanvasToolbar({
  stroke,
  onSetStroke,
  shapeCount,
  onUndo,
  onClear,
  onCancel,
  onDone,
}: Props) {
  return (
    <div
      className="absolute top-3 right-3 flex items-center gap-2 px-2.5 py-1.5 bg-card border border-border rounded-md shadow-md z-local-5"
      onMouseDown={(e) => e.stopPropagation()}
    >
      <span className="text-xs text-muted-foreground">颜色</span>
      {SWATCHES.map((c) => (
        <button
          key={c.value}
          type="button"
          onClick={() => onSetStroke(c.value)}
          aria-label={c.label}
          className={cn(
            "w-[18px] h-[18px] rounded-full border border-border cursor-pointer p-0",
            c.bg,
            stroke === c.value && "border-2 border-foreground",
          )}
        />
      ))}
      <span className="text-xs text-muted-foreground ml-1">{shapeCount} 条</span>
      <Button size="sm" onClick={onUndo} disabled={shapeCount === 0}>
        <Icon name="trash" size={11} /> 撤销
      </Button>
      <Button size="sm" onClick={onClear} disabled={shapeCount === 0}>
        清空
      </Button>
      <Button size="sm" onClick={onCancel}>
        取消
      </Button>
      <Button size="sm" variant="primary" onClick={onDone}>
        完成
      </Button>
    </div>
  );
}
