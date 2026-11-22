import { describe, it, expect } from "vitest";

import type { Psr } from "./triview";
import {
  applyConventionToExtrinsic,
  applyConventionToPositions,
  applyConventionToPsr,
  rotationMatrixFor,
  sniffConventionFromForward,
  unapplyConventionToPsr,
  type LidarAxisConvention,
  type Mat3,
} from "./axisConvention";

const ALL_CONVENTIONS: readonly LidarAxisConvention[] = [
  "iso_8855",
  "ros_rep103",
  "kitti_camera",
  "opencv_camera",
  "apollo",
  "y_forward",
  "sustechpoints_demo",
  "raw",
] as const;

const IDENTITY_CONVENTIONS: readonly LidarAxisConvention[] = [
  "iso_8855",
  "ros_rep103",
  "raw",
] as const;

function mul3(m: Mat3, v: readonly [number, number, number]): [number, number, number] {
  return [
    m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
    m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
    m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
  ];
}

function transpose3(m: Mat3): Mat3 {
  return [m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8]];
}

function mm3(a: Mat3, b: Mat3): Mat3 {
  const r = new Array<number>(9);
  for (let i = 0; i < 3; i += 1) {
    for (let j = 0; j < 3; j += 1) {
      let s = 0;
      for (let k = 0; k < 3; k += 1) s += a[i * 3 + k] * b[k * 3 + j];
      r[i * 3 + j] = s;
    }
  }
  return r as unknown as Mat3;
}

function det3(m: Mat3): number {
  return (
    m[0] * (m[4] * m[8] - m[5] * m[7]) -
    m[1] * (m[3] * m[8] - m[5] * m[6]) +
    m[2] * (m[3] * m[7] - m[4] * m[6])
  );
}

function frobDiff(a: Mat3, b: Mat3): number {
  let s = 0;
  for (let i = 0; i < 9; i += 1) {
    const d = a[i] - b[i];
    s += d * d;
  }
  return Math.sqrt(s);
}

const I3: Mat3 = [1, 0, 0, 0, 1, 0, 0, 0, 1];

describe("rotationMatrixFor · 合法性 (旋转矩阵)", () => {
  it.each(ALL_CONVENTIONS)("%s · det(R) = +1 且 R · Rᵀ = I", (c) => {
    const R = rotationMatrixFor(c);
    expect(det3(R)).toBeCloseTo(1, 6);
    const RRt = mm3(R, transpose3(R));
    expect(frobDiff(RRt, I3)).toBeLessThan(1e-6);
  });
});

describe("rotationMatrixFor · identity convention", () => {
  it.each(IDENTITY_CONVENTIONS)("%s 是 identity", (c) => {
    const R = rotationMatrixFor(c);
    expect(frobDiff(R, I3)).toBeLessThan(1e-12);
  });
});

describe("applyConventionToPositions · 退化", () => {
  it("iso_8855 不改任何点", () => {
    const positions = new Float32Array([1, 2, 3, -4, 5, -6, 0.1, 0.2, 0.3, 0, 0, 0]);
    const before = Array.from(positions);
    applyConventionToPositions(positions, "iso_8855");
    for (let i = 0; i < before.length; i += 1) {
      expect(positions[i]).toBeCloseTo(before[i], 6);
    }
  });
});

describe("applyConventionToPositions · 非 identity 旋转", () => {
  it("apollo: src (1,0,0) → ISO (0,-1,0); src (0,1,0) → ISO (1,0,0)", () => {
    const positions = new Float32Array([1, 0, 0, 0, 1, 0]);
    applyConventionToPositions(positions, "apollo");
    expect(positions[0]).toBeCloseTo(0, 6);
    expect(positions[1]).toBeCloseTo(-1, 6);
    expect(positions[2]).toBeCloseTo(0, 6);
    expect(positions[3]).toBeCloseTo(1, 6);
    expect(positions[4]).toBeCloseTo(0, 6);
    expect(positions[5]).toBeCloseTo(0, 6);
  });

  it("sustechpoints_demo: src (0,1,0) (车后) → ISO (-1,0,0) (车后)", () => {
    const positions = new Float32Array([0, 1, 0]);
    applyConventionToPositions(positions, "sustechpoints_demo");
    expect(positions[0]).toBeCloseTo(-1, 6);
    expect(positions[1]).toBeCloseTo(0, 6);
    expect(positions[2]).toBeCloseTo(0, 6);
  });
});

function makeExtrinsic(R: Mat3, t: readonly [number, number, number]): number[] {
  return [R[0], R[1], R[2], t[0], R[3], R[4], R[5], t[1], R[6], R[7], R[8], t[2], 0, 0, 0, 1];
}

function rotBlock(E: readonly number[]): Mat3 {
  return [E[0], E[1], E[2], E[4], E[5], E[6], E[8], E[9], E[10]];
}

describe("applyConventionToExtrinsic · 数学契约", () => {
  it("apollo: 返回值 = E_src · diag(R_normᵀ, 1), 平移不变, 末行 [0,0,0,1]", () => {
    const a = Math.PI / 6;
    const c = Math.cos(a);
    const s = Math.sin(a);
    const Rsrc: Mat3 = [c, -s, 0, s, c, 0, 0, 0, 1];
    const t: [number, number, number] = [1.5, -2.7, 0.42];
    const Esrc = makeExtrinsic(Rsrc, t);

    const Eiso = applyConventionToExtrinsic(Esrc, "apollo");

    const Rnorm = rotationMatrixFor("apollo");
    const expectRot = mm3(Rsrc, transpose3(Rnorm));
    const gotRot = rotBlock(Eiso);
    expect(frobDiff(gotRot, expectRot)).toBeLessThan(1e-6);

    expect(Eiso[3]).toBeCloseTo(t[0], 6);
    expect(Eiso[7]).toBeCloseTo(t[1], 6);
    expect(Eiso[11]).toBeCloseTo(t[2], 6);

    expect(Eiso[12]).toBeCloseTo(0, 6);
    expect(Eiso[13]).toBeCloseTo(0, 6);
    expect(Eiso[14]).toBeCloseTo(0, 6);
    expect(Eiso[15]).toBeCloseTo(1, 6);
  });

  it("iso_8855: identity convention 不改 extrinsic 旋转块", () => {
    const a = Math.PI / 5;
    const c = Math.cos(a);
    const s = Math.sin(a);
    const Rsrc: Mat3 = [c, 0, s, 0, 1, 0, -s, 0, c];
    const t: [number, number, number] = [0.1, 0.2, 0.3];
    const Esrc = makeExtrinsic(Rsrc, t);
    const Eiso = applyConventionToExtrinsic(Esrc, "iso_8855");
    expect(frobDiff(rotBlock(Eiso), Rsrc)).toBeLessThan(1e-6);
    expect(Eiso[3]).toBeCloseTo(t[0], 6);
    expect(Eiso[7]).toBeCloseTo(t[1], 6);
    expect(Eiso[11]).toBeCloseTo(t[2], 6);
  });
});

describe("applyConventionToExtrinsic · SUSTechPOINTS 实测回归", () => {
  it("front.json row 2 ≈ (-0.033, -0.999, 0.042); 归一化后 row 2 应 ≈ (1, 0, 0)", () => {
    const row2: [number, number, number] = [-0.033, -0.999, 0.042];
    const n = Math.hypot(row2[0], row2[1], row2[2]);
    const r2: [number, number, number] = [row2[0] / n, row2[1] / n, row2[2] / n];
    const seed: [number, number, number] = [1, 0, 0];
    const dot = seed[0] * r2[0] + seed[1] * r2[1] + seed[2] * r2[2];
    const r0Raw: [number, number, number] = [
      seed[0] - dot * r2[0],
      seed[1] - dot * r2[1],
      seed[2] - dot * r2[2],
    ];
    const r0n = Math.hypot(r0Raw[0], r0Raw[1], r0Raw[2]);
    const r0: [number, number, number] = [r0Raw[0] / r0n, r0Raw[1] / r0n, r0Raw[2] / r0n];
    const r1: [number, number, number] = [
      r2[1] * r0[2] - r2[2] * r0[1],
      r2[2] * r0[0] - r2[0] * r0[2],
      r2[0] * r0[1] - r2[1] * r0[0],
    ];
    const Rsrc: Mat3 = [r0[0], r0[1], r0[2], r1[0], r1[1], r1[2], r2[0], r2[1], r2[2]];
    const Esrc = makeExtrinsic(Rsrc, [0, 0, 0]);
    const Eiso = applyConventionToExtrinsic(Esrc, "sustechpoints_demo");

    const got: [number, number, number] = [Eiso[8], Eiso[9], Eiso[10]];
    expect(got[0]).toBeCloseTo(1, 1);
    expect(got[1]).toBeCloseTo(0, 1);
    expect(got[2]).toBeCloseTo(0, 1);
    const dx = got[0] - 1;
    const dy = got[1];
    const dz = got[2];
    expect(Math.hypot(dx, dy, dz)).toBeLessThan(0.1);
  });
});

describe("unapplyConventionToPsr · 行为", () => {
  it("identity convention (iso_8855) 是恒等映射", () => {
    const psr: Psr = {
      center: [3, 1, 2],
      size: [4, 1, 1.5],
      rotation: [0, 0, Math.PI / 3],
    };
    const out = unapplyConventionToPsr(psr, "iso_8855");
    expect(out.center[0]).toBeCloseTo(psr.center[0], 6);
    expect(out.center[1]).toBeCloseTo(psr.center[1], 6);
    expect(out.center[2]).toBeCloseTo(psr.center[2], 6);
    expect(out.size[0]).toBeCloseTo(psr.size[0], 6);
    expect(out.size[1]).toBeCloseTo(psr.size[1], 6);
    expect(out.size[2]).toBeCloseTo(psr.size[2], 6);
    expect(out.rotation[0]).toBeCloseTo(psr.rotation[0], 6);
    expect(out.rotation[1]).toBeCloseTo(psr.rotation[1], 6);
    expect(out.rotation[2]).toBeCloseTo(psr.rotation[2], 6);
  });

  it("apollo 反向: ISO 中心 (2,0,0) (= ISO+X 车前) → src (0,2,0) (= src+Y 方向)", () => {
    const psr: Psr = {
      center: [2, 0, 0],
      size: [4, 1.8, 1.5],
      rotation: [0, 0, 0],
    };
    const out = unapplyConventionToPsr(psr, "apollo");
    expect(out.center[0]).toBeCloseTo(0, 6);
    expect(out.center[1]).toBeCloseTo(2, 6);
    expect(out.center[2]).toBeCloseTo(0, 6);
    expect(out.size[0]).toBeCloseTo(psr.size[0], 6);
    expect(out.size[1]).toBeCloseTo(psr.size[1], 6);
    expect(out.size[2]).toBeCloseTo(psr.size[2], 6);
  });

  it("unapply 实际就是 R_normᵀ · center (对一般 convention 也成立)", () => {
    const psr: Psr = {
      center: [1, 2, 3],
      size: [1, 1, 1],
      rotation: [0, 0, 0],
    };
    const out = unapplyConventionToPsr(psr, "sustechpoints_demo");
    const Rt = transpose3(rotationMatrixFor("sustechpoints_demo"));
    const expected = mul3(Rt, [1, 2, 3]);
    expect(out.center[0]).toBeCloseTo(expected[0], 6);
    expect(out.center[1]).toBeCloseTo(expected[1], 6);
    expect(out.center[2]).toBeCloseTo(expected[2], 6);
  });
});

describe("applyConventionToPsr · 行为", () => {
  it("与 unapply 构成 round-trip", () => {
    const src: Psr = {
      center: [1.5, -2.0, 3.25],
      size: [4.0, 1.8, 1.6],
      rotation: [0.1, -0.2, Math.PI / 4],
    };
    const iso = applyConventionToPsr(src, "apollo");
    const out = unapplyConventionToPsr(iso, "apollo");
    expect(out.center[0]).toBeCloseTo(src.center[0], 6);
    expect(out.center[1]).toBeCloseTo(src.center[1], 6);
    expect(out.center[2]).toBeCloseTo(src.center[2], 6);
    expect(out.size[0]).toBeCloseTo(src.size[0], 6);
    expect(out.size[1]).toBeCloseTo(src.size[1], 6);
    expect(out.size[2]).toBeCloseTo(src.size[2], 6);
    expect(out.rotation[0]).toBeCloseTo(src.rotation[0], 6);
    expect(out.rotation[1]).toBeCloseTo(src.rotation[1], 6);
    expect(out.rotation[2]).toBeCloseTo(src.rotation[2], 6);
  });
});

describe("sniffConventionFromForward · 行为", () => {
  it.each([
    ["iso_8855", "iso_8855"],
    ["ros_rep103", "iso_8855"],
    ["kitti_camera", "kitti_camera"],
    ["opencv_camera", "kitti_camera"],
    ["apollo", "apollo"],
    ["y_forward", "apollo"],
    ["sustechpoints_demo", "sustechpoints_demo"],
  ] as Array<[LidarAxisConvention, LidarAxisConvention]>)(
    "%s row0 → %s",
    (convention, expectedBest) => {
      const m = rotationMatrixFor(convention);
      const result = sniffConventionFromForward(m[0], m[1], m[2]);
      expect(result?.best).toBe(expectedBest);
      expect(result?.score).toBeCloseTo(1, 6);
      expect(
        result?.candidates.some((c) => c.convention === convention && Math.abs(c.score - 1) < 1e-6),
      ).toBe(true);
    },
  );
});
