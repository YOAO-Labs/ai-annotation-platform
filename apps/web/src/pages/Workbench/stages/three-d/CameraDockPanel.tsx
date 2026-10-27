import { useEffect, useMemo, useRef } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Tooltip } from "@/components/ui/Tooltip";
import type { SensorCalibration } from "@/types";

import CameraProjectionView from "./CameraProjectionView";
import { cameraAnchor, type Anchor } from "./geometry/cameraAnchor";
import type { SceneBox } from "./PointCloudScene";

export interface CameraDockPanelProps {
  cameras: readonly {
    role: string;
    name: string;
    image_url?: string | null;
    calibration?: SensorCalibration | null;
  }[];
  boxes: SceneBox[];
  highlightedIds: Set<string>;
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  bestRole?: string | null;
  pointPositions?: Float32Array | null;
  showDepth?: boolean;
  onEnlarge?: (role: string) => void;
  visible?: boolean;
  loading?: boolean;
  error?: string | null;
  /** 恢复相机排列时递增；仅复位图库滚动位置。 */
  resetKey?: number;
}

const CAMERA_DIRECTIONS: Record<Anchor, string> = {
  "top-left": "左前",
  top: "前方",
  "top-right": "右前",
  left: "左侧",
  right: "右侧",
  "bottom-left": "左后",
  bottom: "后方",
  "bottom-right": "右后",
  overflow: "其他朝向",
};
const CAMERA_ORDER = Object.keys(CAMERA_DIRECTIONS) as Anchor[];

export function CameraDockPanel({
  cameras,
  boxes,
  highlightedIds,
  onSelectBox,
  bestRole,
  pointPositions,
  showDepth,
  onEnlarge,
  visible = true,
  loading = false,
  error = null,
  resetKey = 0,
}: CameraDockPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const orderedCameras = useMemo(
    () =>
      cameras
        .map((camera) => ({
          camera,
          anchor: cameraAnchor(camera.calibration, camera.role || camera.name),
        }))
        .sort((a, b) => CAMERA_ORDER.indexOf(a.anchor) - CAMERA_ORDER.indexOf(b.anchor)),
    [cameras],
  );

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [resetKey]);

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col bg-background" data-camera-dock-panel>
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto p-2">
        {loading || error || !cameras.length ? (
          <p className="m-0 px-2 py-6 text-center text-xs text-muted-foreground" role="status">
            {loading ? "加载当前帧相机…" : error || "当前帧没有相机图像"}
          </p>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,240px),1fr))] items-start gap-2">
            {orderedCameras.map(({ camera, anchor }) => (
              <section key={camera.role} className="min-w-0" aria-label={camera.name}>
                <div className="mb-1 flex h-6 items-center justify-between gap-1">
                  <span className="text-xs text-muted-foreground">{CAMERA_DIRECTIONS[anchor]}</span>
                  {onEnlarge && (
                    <Tooltip name={`放大${camera.name}`} side="left">
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        className="w-6 px-0"
                        aria-label={`放大${camera.name}`}
                        onPointerDown={(event) => event.stopPropagation()}
                        onClick={(event) => {
                          event.stopPropagation();
                          onEnlarge(camera.role);
                        }}
                      >
                        <Icon name="zoomIn" size={12} />
                      </Button>
                    </Tooltip>
                  )}
                </div>
                <CameraProjectionView
                  name={camera.name}
                  imageUrl={camera.image_url}
                  calibration={camera.calibration}
                  boxes={boxes}
                  highlightedIds={highlightedIds}
                  onSelectBox={onSelectBox}
                  bestForSelected={camera.role === bestRole}
                  pointPositions={pointPositions}
                  showDepth={showDepth}
                  fitToPanel
                  visible={visible}
                />
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default CameraDockPanel;
