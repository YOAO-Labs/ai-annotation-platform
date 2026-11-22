import type { SensorCalibration } from "@/types";
import type { DepthRaster } from "./depthmap";

export const OCCLUSION_TOL_M = 0.1;

export interface CameraSample {
  calib: SensorCalibration;
  width: number;
  height: number;
  data: Uint8ClampedArray;
}

function projectOne(
  x: number,
  y: number,
  z: number,
  calib: SensorCalibration,
): { u: number; v: number; depth: number } {
  const { extrinsic: e, intrinsic: k, rect } = calib;
  let c0 = e[0] * x + e[1] * y + e[2] * z + e[3];
  let c1 = e[4] * x + e[5] * y + e[6] * z + e[7];
  let c2 = e[8] * x + e[9] * y + e[10] * z + e[11];
  if (rect) {
    const r0 = rect[0] * c0 + rect[1] * c1 + rect[2] * c2 + rect[3];
    const r1 = rect[4] * c0 + rect[5] * c1 + rect[6] * c2 + rect[7];
    const r2 = rect[8] * c0 + rect[9] * c1 + rect[10] * c2 + rect[11];
    c0 = r0;
    c1 = r1;
    c2 = r2;
  }
  const uu = k[0] * c0 + k[1] * c1 + k[2] * c2;
  const vv = k[3] * c0 + k[4] * c1 + k[5] * c2;
  const ww = k[6] * c0 + k[7] * c1 + k[8] * c2;
  if (ww === 0) return { u: NaN, v: NaN, depth: 0 };
  return { u: uu / ww, v: vv / ww, depth: ww };
}

/**
 * 逐点上色。返回与 positions 等长(N*3)的 RGB Float32Array(0..1)。
 * @param positions  点坐标 Float32Array(N*3,lidar/world 系,与标定同系)。
 * @param originalColors  原色(N*3),无相机覆盖的点回退到它;null 则回退中性灰。
 * @param cameras  有标定的相机采样集(空 → 全部点取原色)。
 * @param rasters  可选,与 cameras 一一对应的深度栅格。传入则做 z-test 遮挡剔除:
 *                 候选点深度比该相机栅格在投影格里的最近深度大于 OCCLUSION_TOL_M 时丢弃。
 *                 rasters[i] = null 表示第 i 个相机不做 z-test；
 *                 整个参数为 undefined/null 时所有相机都不做 z-test。
 */
export function colorizePoints(
  positions: Float32Array,
  originalColors: Float32Array | null,
  cameras: CameraSample[],
  rasters?: ReadonlyArray<DepthRaster | null> | null,
): Float32Array {
  const n = (positions.length / 3) | 0;
  const out = new Float32Array(positions.length);
  for (let i = 0; i < n; i++) {
    const x = positions[3 * i];
    const y = positions[3 * i + 1];
    const z = positions[3 * i + 2];

    let best = -1;
    let bestU = 0;
    let bestV = 0;
    let bestScore = Infinity;
    for (let c = 0; c < cameras.length; c++) {
      const cam = cameras[c];
      const { u, v, depth } = projectOne(x, y, z, cam.calib);
      if (depth <= 0) continue;
      if (u < 0 || u >= cam.width || v < 0 || v >= cam.height) continue;
      const r = rasters?.[c] ?? null;
      if (r !== null) {
        const col = Math.min(r.cols - 1, Math.max(0, Math.floor(u / r.cell)));
        const row = Math.min(r.rows - 1, Math.max(0, Math.floor(v / r.cell)));
        const rasterDepth = r.depth[row * r.cols + col];
        if (isFinite(rasterDepth) && depth - rasterDepth > OCCLUSION_TOL_M) continue;
      }
      const du = (u - cam.width / 2) / cam.width;
      const dv = (v - cam.height / 2) / cam.height;
      const score = du * du + dv * dv;
      if (score < bestScore) {
        bestScore = score;
        best = c;
        bestU = u;
        bestV = v;
      }
    }

    if (best >= 0) {
      const cam = cameras[best];
      const px = Math.min(cam.width - 1, Math.max(0, Math.round(bestU)));
      const py = Math.min(cam.height - 1, Math.max(0, Math.round(bestV)));
      const idx = (py * cam.width + px) * 4;
      out[3 * i] = cam.data[idx] / 255;
      out[3 * i + 1] = cam.data[idx + 1] / 255;
      out[3 * i + 2] = cam.data[idx + 2] / 255;
    } else if (originalColors) {
      out[3 * i] = originalColors[3 * i];
      out[3 * i + 1] = originalColors[3 * i + 1];
      out[3 * i + 2] = originalColors[3 * i + 2];
    } else {
      out[3 * i] = 0.5;
      out[3 * i + 1] = 0.5;
      out[3 * i + 2] = 0.5;
    }
  }
  return out;
}

export interface ColorAdjust {
  contrast: number;
  brightness: number;
  gamma: number;
}

export const NEUTRAL_ADJUST: ColorAdjust = { contrast: 1, brightness: 0, gamma: 1 };

export function isNeutralAdjust(a: ColorAdjust): boolean {
  return a.contrast === 1 && a.brightness === 0 && a.gamma === 1;
}

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

export function adjustColors(
  raw: Float32Array,
  adjust: ColorAdjust,
  out?: Float32Array,
): Float32Array {
  const dst = out && out.length === raw.length ? out : new Float32Array(raw.length);
  if (isNeutralAdjust(adjust)) {
    dst.set(raw);
    return dst;
  }

  const invGamma = 1 / adjust.gamma;
  const lut = new Float32Array(256);
  for (let i = 0; i < 256; i += 1) {
    const gammaValue = Math.pow(i / 255, invGamma);
    const contrasted = (gammaValue - 0.5) * adjust.contrast + 0.5;
    lut[i] = clamp01(contrasted + adjust.brightness);
  }
  for (let i = 0; i < raw.length; i += 1) {
    dst[i] = lut[Math.round(clamp01(raw[i]) * 255)];
  }
  return dst;
}
