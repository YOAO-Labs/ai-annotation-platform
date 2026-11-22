import { iouShape, type ShapeForIoU } from "./iou";

export type Geom = { x: number; y: number; w: number; h: number };

interface ToastInput {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

export const MIN_BOX_SIZE = 0.005;
export const DUP_IOU = 0.95;

export function guardDrawnBox(
  geo: Geom,
  existing: ReadonlyArray<ShapeForIoU>,
  pushToast: (toast: ToastInput) => void,
): Geom | null {
  const x = Math.max(0, Math.min(1, geo.x));
  const y = Math.max(0, Math.min(1, geo.y));
  const w = Math.min(1 - x, geo.w);
  const h = Math.min(1 - y, geo.h);

  if (w < MIN_BOX_SIZE || h < MIN_BOX_SIZE) {
    pushToast({ msg: "框太小未保存", sub: "拖动到至少 0.5% × 0.5%", kind: "error" });
    return null;
  }

  const g = { x, y, w, h };
  if (existing.some((u) => iouShape(u, g) > DUP_IOU)) {
    pushToast({ msg: "疑似重复，未保存", sub: "与已有框几乎完全重叠", kind: "warning" });
    return null;
  }

  return g;
}
