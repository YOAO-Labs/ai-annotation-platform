import { CopyableError } from "@/components/ui/CopyableError";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { usePolygonBoundaryTrace } from "./usePolygonBoundaryTrace";
import type { BoundaryDirection } from "./shared/geometry/polygonBoundaryTrace";

export function PolygonBoundaryTraceControls({
  controller,
  onBegin,
}: {
  controller: ReturnType<typeof usePolygonBoundaryTrace>;
  onBegin: () => void;
}) {
  const { trace, cancel, choose, confirm } = controller;
  return (
    <div
      data-workbench-polygon-trace
      data-testid="polygon-boundary-trace"
      className="absolute left-3 top-3 z-local-overlay max-w-full rounded border border-border bg-card p-2 text-xs text-foreground shadow-sm"
      onKeyDown={(event) => {
        if (event.key === "Escape" && trace) {
          event.preventDefault();
          event.stopPropagation();
          cancel();
        }
      }}
    >
      {!trace ? (
        <Button size="xs" onClick={onBegin}>
          <Icon name="polygon" size={12} />
          沿已有边界
        </Button>
      ) : (
        <div className="flex max-w-xs flex-col gap-2">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">沿已有边界</span>
            <Button size="xs" variant="ghost" onClick={cancel}>
              取消追踪
            </Button>
          </div>
          <p role="status" className="m-0 text-2xs text-muted-foreground">
            {trace.paths
              ? "选择路径后确认追加；Esc 取消预览。"
              : trace.source
                ? "在同一条边界上点击终点。"
                : "在已保存的多边形边界上点击起点。"}
          </p>
          {trace.paths && (
            <>
              <div className="flex flex-wrap gap-2" role="group" aria-label="边界路径">
                {(["clockwise", "counterclockwise"] as BoundaryDirection[]).map((direction) => {
                  const paths = trace.paths!;
                  const shorter =
                    direction === "clockwise"
                      ? paths.clockwise.length <= paths.counterclockwise.length
                      : paths.counterclockwise.length < paths.clockwise.length;
                  return (
                    <Button
                      key={direction}
                      size="xs"
                      variant={trace.direction === direction ? "primary" : "default"}
                      aria-pressed={trace.direction === direction}
                      disabled={trace.busy}
                      onClick={() => choose(direction)}
                    >
                      {direction === "clockwise" ? "顺时针" : "逆时针"} ·{" "}
                      {paths[direction].points.length} 点{shorter ? " · 较短" : ""}
                    </Button>
                  );
                })}
              </div>
              <Button
                size="xs"
                variant="primary"
                disabled={trace.busy}
                onClick={() => void confirm()}
              >
                {trace.busy ? "正在核验来源…" : "确认追加"}
              </Button>
            </>
          )}
          {trace.error && (
            <p role="alert" className="m-0 text-2xs text-status-danger">
              <CopyableError message={trace.error} />
            </p>
          )}
        </div>
      )}
    </div>
  );
}
