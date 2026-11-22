import { acquireCameraImageAddress } from "./pointCloudAssetCache";
import { originalOwnerKey } from "@/api/originals";
import type { CSSProperties } from "react";
import * as THREE from "three";
import type { Box3DGeometry, SensorCalibration } from "@/types";
import type { Psr } from "./geometry/triview";
import { cameraAnchor, type Anchor } from "./geometry/cameraAnchor";
import type { CameraSample } from "./geometry/colorize";
import type { LidarAxisConvention } from "./geometry/axisConvention";
import { LIDAR_BOX_3D_TOOL_UNIT } from "./geometry/box3dAttributes";
export const DEFAULT_BOX_SIZE: [number, number, number] = [4.0, 1.8, 1.6];
export const IDENTITY_MATRIX = new THREE.Matrix4();
export const SEED_FALLBACK_RANGE_M = 12;
export const CAMERA_AUTO_COLLAPSE_WIDTH = 1366;
export const CAMERA_STACK_VISIBLE = 2;
export type ThreeDLayoutPreset = "box-refinement" | "sensor-fusion" | "point-segmentation";

export const THREE_D_LAYOUT_PRESETS: ReadonlyArray<{
  id: ThreeDLayoutPreset;
  label: string;
  description: string;
}> = [
  { id: "box-refinement", label: "框体精修", description: "显示三视图，隐藏相机面板" },
  { id: "sensor-fusion", label: "传感器融合", description: "显示相机，保留当前呈现模式" },
  { id: "point-segmentation", label: "点级分割", description: "隐藏三视图和相机面板" },
];

export type BoxSelectRectVars = CSSProperties & {
  "--rect-l": string;
  "--rect-t": string;
  "--rect-w": string;
  "--rect-h": string;
};

export function sortedIndices(indices: Iterable<number>): number[] {
  return [...indices].sort((a, b) => a - b);
}

export function resolveBox3dDefaultSize(
  value?: [number, number, number] | null,
): [number, number, number] {
  if (value && value.length === 3 && value.every((n) => Number.isFinite(n) && n > 0)) {
    return [value[0], value[1], value[2]];
  }
  return DEFAULT_BOX_SIZE;
}
export const LIDAR_TOOL_UNIT = LIDAR_BOX_3D_TOOL_UNIT;
export const POINT_MASK_TOOL_UNIT = "point_mask_3d";

export function boxGeometryFromPsr(psr: Psr, convention: LidarAxisConvention): Box3DGeometry {
  return {
    type: "box_3d",
    center: [psr.center[0], psr.center[1], psr.center[2]],
    size: [psr.size[0], psr.size[1], psr.size[2]],
    rotation: [psr.rotation[0], psr.rotation[1], psr.rotation[2]],
    convention_at_create: convention,
  };
}

export function geometryConvention(
  geometry: unknown,
  fallback: LidarAxisConvention,
): LidarAxisConvention {
  const g = geometry as { convention_at_create?: LidarAxisConvention | null } | null;
  return g?.convention_at_create ?? fallback;
}

export async function loadCameraSample(
  imageUrl: string | null | undefined,
  calib: SensorCalibration,
  signal?: AbortSignal,
): Promise<CameraSample | null> {
  if (!imageUrl || signal?.aborted) return null;
  const owner = originalOwnerKey();
  let address: Awaited<ReturnType<typeof acquireCameraImageAddress>>;
  try {
    address = await acquireCameraImageAddress(imageUrl, signal);
  } catch {
    return null;
  }
  if (signal?.aborted || owner !== originalOwnerKey()) {
    address.release();
    return null;
  }
  return new Promise<CameraSample | null>((resolve) => {
    const img = new Image();
    let settled = false;
    const finish = (sample: CameraSample | null) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", handleAbort);
      img.onload = null;
      img.onerror = null;
      address.release();
      resolve(owner === originalOwnerKey() && !signal?.aborted ? sample : null);
    };
    const handleAbort = () => {
      img.src = "";
      finish(null);
    };
    if (signal?.aborted) {
      finish(null);
      return;
    }
    signal?.addEventListener("abort", handleAbort, { once: true });
    img.crossOrigin = "anonymous";
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx) return finish(null);
      try {
        ctx.drawImage(img, 0, 0);
        const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
        finish({ calib, width: canvas.width, height: canvas.height, data });
      } catch {
        finish(null);
      }
    };
    img.onerror = () => finish(null);
    img.src = address.url;
  });
}

export type PsrField = "cx" | "cy" | "cz" | "l" | "w" | "h" | "yaw" | "pitch" | "roll";
export const PSR_FIELDS: PsrField[] = ["cx", "cy", "cz", "l", "w", "h", "yaw", "pitch", "roll"];
export const SIZE_FIELDS = new Set<PsrField>(["l", "w", "h"]);
export const PSR_GROUPS: {
  label: string;
  keys: PsrField[];
  step: number;
  min?: number;
  reset?: boolean;
}[] = [
  { label: "中心 (m)", keys: ["cx", "cy", "cz"], step: 0.1 },
  { label: "尺寸 长宽高 (m)", keys: ["l", "w", "h"], step: 0.1, min: 0.1 },
  { label: "朝向 偏航/俯仰/翻滚 (°)", keys: ["yaw", "pitch", "roll"], step: 1, reset: true },
];
export const fmtNum = (n: number) => String(+n.toFixed(3));
export function psrToForm(b: {
  center: readonly number[];
  size: readonly number[];
  rotation: readonly number[];
}): Record<PsrField, string> {
  return {
    cx: fmtNum(b.center[0]),
    cy: fmtNum(b.center[1]),
    cz: fmtNum(b.center[2]),
    l: fmtNum(b.size[0]),
    w: fmtNum(b.size[1]),
    h: fmtNum(b.size[2]),
    yaw: fmtNum((b.rotation[2] * 180) / Math.PI),
    pitch: fmtNum((b.rotation[1] * 180) / Math.PI),
    roll: fmtNum((b.rotation[0] * 180) / Math.PI),
  };
}

export function isPsrFieldBad(field: PsrField, value: string): boolean {
  const n = Number(value);
  return value.trim() === "" || !Number.isFinite(n) || (SIZE_FIELDS.has(field) && n <= 0);
}

export function parsePsrForm(form: Record<PsrField, string>): {
  values: Record<PsrField, number>;
  valid: boolean;
} {
  const values = {} as Record<PsrField, number>;
  for (const k of PSR_FIELDS) values[k] = Number(form[k]);
  const valid =
    PSR_FIELDS.every((k) => form[k].trim() !== "" && Number.isFinite(values[k])) &&
    values.l > 0 &&
    values.w > 0 &&
    values.h > 0;
  return { values, valid };
}

export function psrFormToGeometry(
  values: Record<PsrField, number>,
  convention: LidarAxisConvention,
): Box3DGeometry {
  const deg = Math.PI / 180;
  return boxGeometryFromPsr(
    {
      center: [values.cx, values.cy, values.cz],
      size: [values.l, values.w, values.h],
      rotation: [values.roll * deg, values.pitch * deg, values.yaw * deg],
    },
    convention,
  );
}

export function frontCameraForward(
  cams: { calibration?: SensorCalibration | null; role: string; name: string }[],
): [number, number] | null {
  const front = cams.find((c) => cameraAnchor(c.calibration, c.role || c.name) === "top");
  const e = front?.calibration?.extrinsic;
  if (!e) return null;
  const x = e[8];
  const y = e[9];
  const n = Math.hypot(x, y);
  return n < 1e-3 ? null : [x / n, y / n];
}

export const ANCHOR_CLASS: Record<Anchor, string> = {
  top: "top-3 left-0 right-0 justify-center flex-row",
  bottom: "bottom-3 left-0 right-0 justify-center flex-row",
  left: "left-3 top-[var(--top-toolbar-height)] bottom-3 justify-center flex-col",
  right: "right-3 top-[var(--top-toolbar-height)] bottom-3 justify-center flex-col",
  "top-left": "top-[30%] left-3 flex-col",
  "top-right": "top-[30%] right-3 flex-col",
  "bottom-left": "bottom-[30%] left-3 flex-col",
  "bottom-right": "bottom-[30%] right-3 flex-col",
  overflow: "bottom-3 left-0 right-0 justify-center flex-row",
};

export function enlargedCameraNavigationDirection(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
): -1 | 1 | null {
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return null;
  if (event.key === "ArrowLeft") return -1;
  if (event.key === "ArrowRight") return 1;
  return null;
}
