import { CopyableError } from "@/components/ui/CopyableError";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { usePolygonSlice } from "./usePolygonSlice";
import { sliceSignedArea } from "./shared/geometry/polygonSlice";

export function PolygonSliceControls({
  controller,
}: {
  controller: ReturnType<typeof usePolygonSlice>;
}) {
  const session = controller.session;
  if (!session) return null;
  const areas = session.preview?.map((part) => Math.abs(sliceSignedArea(part.points)));
  return (
    <div
      data-workbench-polygon-slice
      data-state="open"
      data-testid="polygon-slice-controls"
      data-slice-preview={session.preview ? JSON.stringify(session.preview) : undefined}
      className="absolute left-3 top-3 z-local-overlay max-w-sm rounded-md border border-border bg-card p-3 text-xs text-card-foreground shadow-md"
    >
      <div className="mb-2 flex items-center gap-2 font-medium">
        <Icon name="scissors" size={14} />
        切割多边形
      </div>
      <p className="mb-2 text-2xs text-muted-foreground">
        {session.preview && areas
          ? `预览 2 块 · 保留来源 ${((100 * areas[0]) / (areas[0] + areas[1])).toFixed(1)}% · 新对象 ${((100 * areas[1]) / (areas[0] + areas[1])).toFixed(1)}%`
          : `从对象外或边界逐点绘制切线 · ${session.points.length}/256 点`}
      </p>
      <p className="mb-2 text-2xs text-muted-foreground">
        {session.preview ? "Enter 确认切割 · Esc 取消" : "Enter 预览 · Backspace 撤一点 · Esc 取消"}
      </p>
      {session.error && (
        <p role="alert" className="mb-2 text-2xs text-status-danger">
          <CopyableError message={session.error} />
        </p>
      )}
      <div className="flex gap-2">
        {session.preview ? (
          <Button
            size="sm"
            variant="primary"
            disabled={session.busy || session.invalid}
            onClick={() => void controller.confirm()}
          >
            {session.busy ? "正在切割…" : session.request ? "重试切割" : "确认切割"}
          </Button>
        ) : (
          <Button
            size="sm"
            variant="primary"
            disabled={session.points.length < 2 || session.invalid}
            onClick={controller.preview}
          >
            预览切割
          </Button>
        )}
        {!session.request && (
          <Button
            size="sm"
            variant="default"
            disabled={session.invalid || (!session.preview && !session.points.length)}
            onClick={controller.back}
          >
            {session.preview ? "修改切线" : "撤回一点"}
          </Button>
        )}
        <Button size="sm" variant="ghost" disabled={session.busy} onClick={controller.cancel}>
          取消切割
        </Button>
      </div>
    </div>
  );
}
