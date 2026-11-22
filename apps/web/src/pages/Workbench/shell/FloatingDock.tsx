import { Icon } from "@/components/ui/Icon";

interface FloatingDockProps {
  scale: number;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFit: () => void;
  showHistory?: boolean;
}

export function FloatingDock({
  scale,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onZoomIn,
  onZoomOut,
  onFit,
  showHistory = true,
}: FloatingDockProps) {
  return (
    <div className="absolute bottom-3 right-3 z-dock flex select-none items-center gap-0.5 rounded-lg border border-border bg-card/90 p-1.5 shadow-lg backdrop-blur-sm">
      {showHistory && (
        <>
          <DockButton onClick={onUndo} disabled={!canUndo} title="撤销 (Ctrl+Z)">
            <Icon name="chevLeft" size={14} />
          </DockButton>
          <DockButton onClick={onRedo} disabled={!canRedo} title="重做 (Ctrl+Shift+Z)">
            <Icon name="chevRight" size={14} />
          </DockButton>
          <Sep />
        </>
      )}
      <DockButton onClick={onZoomOut} title="缩小">
        <Icon name="zoomOut" size={14} />
      </DockButton>
      <span className="mono min-w-[46px] text-center text-sm font-medium tracking-[0.2px] text-foreground">
        {Math.round(scale * 100)}%
      </span>
      <DockButton onClick={onZoomIn} title="放大">
        <Icon name="zoomIn" size={14} />
      </DockButton>
      <Sep />
      <DockButton onClick={onFit} title="适应视口（双击空白）" variant="fit">
        适应
      </DockButton>
    </div>
  );
}

interface DockButtonProps {
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  children: React.ReactNode;
  variant?: "fit";
}
function DockButton({ onClick, disabled, title, children, variant }: DockButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={[
        "inline-flex h-7 min-w-[28px] cursor-pointer appearance-none items-center justify-center rounded border-0 bg-transparent p-0 text-foreground transition-colors enabled:hover:bg-muted",
        disabled ? "cursor-default text-muted-foreground/60" : "",
        variant === "fit" ? "px-2.5 text-xs font-medium" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </button>
  );
}

function Sep() {
  return <div className="mx-0.5 h-4 w-px bg-border" />;
}
