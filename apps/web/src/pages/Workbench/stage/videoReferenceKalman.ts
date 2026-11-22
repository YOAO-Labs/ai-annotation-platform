import type { VideoReferencePreset } from "./videoReferencePredict";

export interface ReferenceKalmanKeyframe {
  frame: number;
  cx: number;
  cy: number;
  w: number;
  h: number;
}

export interface ReferenceKalmanResult {
  cx: number;
  cy: number;
  w: number;
  h: number;
  sx: number;
  sy: number;
  sw: number;
  sh: number;
}

interface NoiseParams {
  qPos: number;
  qSize: number;
  r: number;
}

const PRESETS: Record<VideoReferencePreset, NoiseParams> = {
  stable: { qPos: 1e-6, qSize: 2.5e-7, r: 1e-4 },
  agile: { qPos: 1e-4, qSize: 2.5e-5, r: 1e-5 },
};

const POS_VAR0 = 1e-4;
const VEL_VAR0 = 1e-3;

interface Filter1D {
  p: number;
  v: number;
  P00: number;
  P01: number;
  P11: number;
}

function makeFilter(p0: number, v0: number): Filter1D {
  return { p: p0, v: v0, P00: POS_VAR0, P01: 0, P11: VEL_VAR0 };
}

function predict(f: Filter1D, dt: number, q: number): void {
  f.p += f.v * dt;
  const P00 = f.P00 + dt * (2 * f.P01 + dt * f.P11);
  const P01 = f.P01 + dt * f.P11;
  const P11 = f.P11;
  const dt2 = dt * dt;
  const dt3 = dt2 * dt;
  f.P00 = P00 + (q * dt3) / 3;
  f.P01 = P01 + (q * dt2) / 2;
  f.P11 = P11 + q * dt;
}

function update(f: Filter1D, z: number, r: number): void {
  const S = f.P00 + r;
  const k0 = f.P00 / S;
  const k1 = f.P01 / S;
  const y = z - f.p;
  f.p += k0 * y;
  f.v += k1 * y;
  const P00 = f.P00;
  const P01 = f.P01;
  f.P00 = P00 - k0 * P00;
  f.P01 = P01 - k0 * P01;
  f.P11 = f.P11 - k1 * P01;
}

export function runReferenceKalman(
  keyframes: ReferenceKalmanKeyframe[],
  targetFrame: number,
  preset: VideoReferencePreset,
): ReferenceKalmanResult {
  const { qPos, qSize, r } = PRESETS[preset];
  const k0 = keyframes[0];
  const k1 = keyframes[1];
  const span = Math.max(1, k1.frame - k0.frame);

  const dims = [
    {
      get: (kf: ReferenceKalmanKeyframe) => kf.cx,
      q: qPos,
      f: makeFilter(k0.cx, (k1.cx - k0.cx) / span),
    },
    {
      get: (kf: ReferenceKalmanKeyframe) => kf.cy,
      q: qPos,
      f: makeFilter(k0.cy, (k1.cy - k0.cy) / span),
    },
    {
      get: (kf: ReferenceKalmanKeyframe) => kf.w,
      q: qSize,
      f: makeFilter(k0.w, (k1.w - k0.w) / span),
    },
    {
      get: (kf: ReferenceKalmanKeyframe) => kf.h,
      q: qSize,
      f: makeFilter(k0.h, (k1.h - k0.h) / span),
    },
  ];

  for (let i = 1; i < keyframes.length; i++) {
    const dt = Math.max(1, keyframes[i].frame - keyframes[i - 1].frame);
    for (const d of dims) {
      predict(d.f, dt, d.q);
      update(d.f, d.get(keyframes[i]), r);
    }
  }

  const dtFinal = Math.max(0, targetFrame - keyframes[keyframes.length - 1].frame);
  for (const d of dims) predict(d.f, dtFinal, d.q);

  return {
    cx: dims[0].f.p,
    cy: dims[1].f.p,
    w: dims[2].f.p,
    h: dims[3].f.p,
    sx: Math.sqrt(Math.max(0, dims[0].f.P00)),
    sy: Math.sqrt(Math.max(0, dims[1].f.P00)),
    sw: Math.sqrt(Math.max(0, dims[2].f.P00)),
    sh: Math.sqrt(Math.max(0, dims[3].f.P00)),
  };
}
