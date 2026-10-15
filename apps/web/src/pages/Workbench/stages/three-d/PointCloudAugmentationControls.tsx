import type { PointCloudAugmentationSource, PointCloudDisplayStyle } from "@/api/auth";
import { Button } from "@/components/ui/Button";
import {
  POINT_CLOUD_AUGMENTATION_OPTIONS,
  isPointCloudAugmentationSource,
} from "../../state/pointCloudAugmentation";

const SELECT =
  "min-w-0 max-w-full rounded-sm border border-border bg-background px-1.5 py-1 text-xs text-foreground";

export function PointCloudAugmentationControls({
  source,
  displayStyle,
  loaded,
  compare,
  sourceColor,
  cameras,
  cameraRole,
  legacySurface,
  onSourceChange,
  onStyleChange,
  onCompareChange,
  onSourceColorChange,
  onCameraChange,
}: {
  source: PointCloudAugmentationSource;
  displayStyle: PointCloudDisplayStyle;
  loaded: boolean;
  compare: boolean;
  sourceColor: boolean;
  cameras: readonly { role: string; name: string }[];
  cameraRole: string | null;
  legacySurface: boolean;
  onSourceChange: (value: PointCloudAugmentationSource) => void;
  onStyleChange: (value: PointCloudDisplayStyle) => void;
  onCompareChange: (value: boolean) => void;
  onSourceColorChange: (value: boolean) => void;
  onCameraChange: (value: string) => void;
}) {
  const cameraMissing = !!cameraRole && !cameras.some((camera) => camera.role === cameraRole);
  return (
    <div
      className="pointer-events-auto flex max-w-full flex-col gap-1 rounded-md border border-border bg-card p-1.5 text-xs text-muted-foreground"
      data-testid="pointcloud-augmentation-controls"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <label className="flex min-w-0 items-center gap-1.5">
          点云补强
          <select
            className={SELECT}
            aria-label="点云补强"
            data-testid="pointcloud-augmentation-source"
            value={source}
            disabled={!loaded}
            onChange={(event) => {
              if (isPointCloudAugmentationSource(event.target.value))
                onSourceChange(event.target.value);
            }}
          >
            {POINT_CLOUD_AUGMENTATION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <Button
          type="button"
          size="xs"
          variant={compare ? "primary" : "ghost"}
          aria-pressed={compare}
          disabled={source === "none"}
          onClick={() => onCompareChange(!compare)}
          data-testid="pointcloud-measured-comparison"
        >
          实测对照
        </Button>
        <label className="flex items-center gap-1.5">
          外观
          <select
            className={SELECT}
            aria-label="点云外观"
            value={displayStyle}
            disabled={!loaded}
            onChange={(event) =>
              onStyleChange(event.target.value === "surface" ? "surface" : "points")
            }
          >
            <option value="points">点</option>
            <option value="surface">表面</option>
          </select>
        </label>
      </div>
      {source === "vggt_omega" && (
        <div className="flex flex-wrap items-center gap-1.5">
          <label className="flex min-w-0 items-center gap-1.5">
            AI 方向
            <select
              className={SELECT}
              aria-label="AI 相机方向"
              data-testid="pointcloud-augmentation-camera"
              value={cameraRole ?? ""}
              disabled={cameras.length === 0 && !cameraRole}
              onChange={(event) => onCameraChange(event.target.value)}
            >
              {!cameraRole && <option value="">无可用相机</option>}
              {cameraMissing && <option value={cameraRole!}>{cameraRole}（当前帧缺失）</option>}
              {cameras.map((camera) => (
                <option key={camera.role} value={camera.role}>
                  {camera.name || camera.role}
                </option>
              ))}
            </select>
          </label>
          <Button
            type="button"
            size="xs"
            variant={sourceColor ? "primary" : "ghost"}
            aria-pressed={sourceColor}
            onClick={() => onSourceColorChange(!sourceColor)}
            data-testid="pointcloud-augmentation-source-color"
          >
            来源着色
          </Button>
          <span className="text-status-info-alt">
            实验 · 单方向参考{sourceColor ? " · 紫色为 AI 辅助" : ""}
          </span>
        </div>
      )}
      {legacySurface && <span>已保留旧表面设置的前后 1 帧；选择来源后外观与补强分开。</span>}
    </div>
  );
}
