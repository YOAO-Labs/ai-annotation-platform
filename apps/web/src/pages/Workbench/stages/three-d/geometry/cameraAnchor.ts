import type { SensorCalibration } from "@/types";

export type Anchor =
  | "top"
  | "bottom"
  | "left"
  | "right"
  | "top-left"
  | "top-right"
  | "bottom-left"
  | "bottom-right"
  | "overflow";

const NAME_RULES: ReadonlyArray<readonly [RegExp, Anchor]> = [
  [/front[_-]?left|left[_-]?front/, "top-left"],
  [/front[_-]?right|right[_-]?front/, "top-right"],
  [/(rear|back)[_-]?left|left[_-]?(rear|back)/, "bottom-left"],
  [/(rear|back)[_-]?right|right[_-]?(rear|back)/, "bottom-right"],
  [/front|forward/, "top"],
  [/rear|back/, "bottom"],
  [/left/, "left"],
  [/right/, "right"],
];

function anchorByName(idOrName: string): Anchor | null {
  const s = idOrName.toLowerCase();
  for (const [re, anchor] of NAME_RULES) {
    if (re.test(s)) return anchor;
  }
  return null;
}

function azimuthToAnchor(azDeg: number): Anchor {
  let a = azDeg;
  while (a <= -180) a += 360;
  while (a > 180) a -= 360;
  const abs = Math.abs(a);
  if (abs <= 22.5) return "top";
  if (abs >= 157.5) return "bottom";
  if (a > 22.5 && a < 67.5) return "top-left";
  if (a >= 67.5 && a <= 112.5) return "left";
  if (a > 112.5 && a < 157.5) return "bottom-left";
  if (a < -22.5 && a > -67.5) return "top-right";
  if (a <= -67.5 && a >= -112.5) return "right";
  return "bottom-right";
}

function anchorByExtrinsic(calib: SensorCalibration | null | undefined): Anchor | null {
  const e = calib?.extrinsic;
  if (!e || e.length < 11) return null;
  const fx = e[8];
  const fy = e[9];
  if (!isFinite(fx) || !isFinite(fy)) return null;
  if (Math.hypot(fx, fy) < 1e-3) return null;
  return azimuthToAnchor((Math.atan2(fy, fx) * 180) / Math.PI);
}

export function cameraAnchor(
  calib: SensorCalibration | null | undefined,
  idOrName: string,
): Anchor {
  return anchorByName(idOrName) ?? anchorByExtrinsic(calib) ?? "overflow";
}
