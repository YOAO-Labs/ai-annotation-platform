import { describe, it, expect } from "vitest";
import type { SensorCalibration } from "@/types";
import { psrToCorners } from "./box3d";
import { projectPoints, unprojectPixelAtDepth, BOX_EDGES } from "./projection";

function refMatmul(m: number[], x: number[], vl: number): number[] {
  const ret: number[] = [];
  const resL = m.length / vl;
  for (let r = 0; r < resL; r++) {
    ret[r] = 0;
    for (let i = 0; i < vl; i++) {
      ret[r] += m[r * vl + i] * x[i];
    }
  }
  return ret;
}

function refProject(
  p: readonly [number, number, number],
  calib: SensorCalibration,
): { pixel: [number, number]; visible: boolean } {
  const homo = [p[0], p[1], p[2], 1];
  let imgpos = refMatmul(calib.extrinsic as unknown as number[], homo, 4);
  if (calib.rect) {
    imgpos = refMatmul(calib.rect as number[], imgpos, 4);
  }
  const imgpos3 = [imgpos[0], imgpos[1], imgpos[2]];
  const imgpos2 = refMatmul(calib.intrinsic as unknown as number[], imgpos3, 3);
  const w = imgpos2[2];
  const pixel: [number, number] = [imgpos2[0] / w, imgpos2[1] / w];
  return { pixel, visible: w > 0 };
}

const FRONT_CALIB: SensorCalibration = {
  extrinsic: [
    -0.9994466143126584, 0.033033376071303994, -0.003906559137689193, 0.20487898588180542,
    0.0025198193977806005, -0.0419178508124942, -0.9991178830816032, 0.0013696063542738557,
    -0.033167991334523576, -0.9985748293686324, 0.04181141593201179, -0.10943480581045151, 0, 0, 0,
    1,
  ] as unknown as SensorCalibration["extrinsic"],
  intrinsic: [
    1210.062981, 0.0, 1022.429903, 0.0, 1205.8507139999999, 792.541644, 0.0, 0.0, 1.0,
  ] as unknown as SensorCalibration["intrinsic"],
};

const BOX27 = {
  center: [36.659412470409734, -34.404139500162444, 0.8085990000000003] as const,
  size: [4.5, 1.8, 1.5] as const,
  rotation: [0, 0, 6.04508466163526] as const,
};

describe("projectPoints — SUSTechPOINTS oracle 对拍", () => {
  it("(a) PSR→像素端到端: yaw-only 框 8 角点逐一与 oracle 一致", () => {
    const corners = psrToCorners(BOX27.center, BOX27.size, BOX27.rotation);
    const got = projectPoints(corners, FRONT_CALIB);

    corners.forEach((c, i) => {
      const ref = refProject([c.x, c.y, c.z], FRONT_CALIB);
      expect(got.pixels[i][0]).toBeCloseTo(ref.pixel[0], 4);
      expect(got.pixels[i][1]).toBeCloseTo(ref.pixel[1], 4);
      expect(got.visible[i]).toBe(ref.visible);
    });
    expect(got.visible.every((v) => v)).toBe(true);
  });

  it("(b) 投影链隔离对拍: 任意世界点 (含相机前/后) 像素+visible 均与 oracle 一致", () => {
    const pts: ReadonlyArray<readonly [number, number, number]> = [
      [10, 0, 0],
      [5, -3, 1.2],
      [-8, 4, 2],
      [0, 0, 0],
      [36.66, -34.4, 0.8],
    ];
    const got = projectPoints(pts, FRONT_CALIB);
    pts.forEach((p, i) => {
      const ref = refProject(p, FRONT_CALIB);
      if (Number.isNaN(ref.pixel[0])) {
        expect(got.pixels[i][0]).toBeNaN();
        expect(got.pixels[i][1]).toBeNaN();
      } else {
        expect(got.pixels[i][0]).toBeCloseTo(ref.pixel[0], 5);
        expect(got.pixels[i][1]).toBeCloseTo(ref.pixel[1], 5);
      }
      expect(got.visible[i]).toBe(ref.visible);
    });
  });
});

describe("projectPoints — behind-camera 剔除 (identity extrinsic + 简单 intrinsic)", () => {
  const SIMPLE_CALIB: SensorCalibration = {
    extrinsic: [
      1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
    ] as unknown as SensorCalibration["extrinsic"],
    intrinsic: [1000, 0, 500, 0, 1000, 500, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
  };

  it("(c) 相机后方点 visible=false, 前方点 visible=true 且像素=手算 golden", () => {
    const front: [number, number, number] = [1, 2, 5];
    const back: [number, number, number] = [1, 2, -5];
    const got = projectPoints([front, back], SIMPLE_CALIB);

    expect(got.visible[0]).toBe(true);
    expect(got.pixels[0][0]).toBeCloseTo(700, 6);
    expect(got.pixels[0][1]).toBeCloseTo(900, 6);

    expect(got.visible[1]).toBe(false);
    expect(got.pixels[1][0]).toBeCloseTo((1000 * 1) / -5 + 500, 6);
    expect(got.pixels[1][1]).toBeCloseTo((1000 * 2) / -5 + 500, 6);
  });
});

it("与后端共享的 KITTI 手算 oracle bbox 对拍", () => {
  const calibration: SensorCalibration = {
    extrinsic: [
      0, -1, 0, 0, 0, 0, -1, 0, 1, 0, 0, 0, 0, 0, 0, 1,
    ] as unknown as SensorCalibration["extrinsic"],
    intrinsic: [800, 0, 640, 0, 800, 360, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
  };
  const corners = psrToCorners([10, 0, 0], [4, 2, 2], [0, 0, 0]);
  const projected = projectPoints(corners, calibration);
  const visible = projected.pixels.filter((_pixel, index) => projected.visible[index]);
  const bbox = [
    Math.min(...visible.map((pixel) => pixel[0])),
    Math.min(...visible.map((pixel) => pixel[1])),
    Math.max(...visible.map((pixel) => pixel[0])),
    Math.max(...visible.map((pixel) => pixel[1])),
  ];

  expect(bbox).toEqual([540, 260, 740, 460]);
});

describe("projectPoints — rect 处理", () => {
  const IDENTITY_RECT = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

  it("(d1) identity rect 结果应与无 rect 完全一致", () => {
    const corners = psrToCorners(BOX27.center, BOX27.size, BOX27.rotation);
    const noRect = projectPoints(corners, FRONT_CALIB);
    const withIdentityRect = projectPoints(corners, {
      ...FRONT_CALIB,
      rect: IDENTITY_RECT as unknown as SensorCalibration["rect"],
    });
    corners.forEach((_, i) => {
      expect(withIdentityRect.pixels[i][0]).toBeCloseTo(noRect.pixels[i][0], 9);
      expect(withIdentityRect.pixels[i][1]).toBeCloseTo(noRect.pixels[i][1], 9);
      expect(withIdentityRect.visible[i]).toBe(noRect.visible[i]);
    });
  });

  it("(d2) 非平凡 rect 结果与 oracle 一致", () => {
    const rect = [
      0.999, -0.01, 0.005, 0.02, 0.01, 0.9998, -0.002, -0.01, -0.005, 0.002, 1.0, 0.03, 0, 0, 0, 1,
    ];
    const calib: SensorCalibration = {
      ...FRONT_CALIB,
      rect: rect as unknown as SensorCalibration["rect"],
    };
    const pts: ReadonlyArray<readonly [number, number, number]> = [
      [10, 0, 0],
      [5, -3, 1.2],
      [36.66, -34.4, 0.8],
    ];
    const got = projectPoints(pts, calib);
    pts.forEach((p, i) => {
      const ref = refProject(p, calib);
      expect(got.pixels[i][0]).toBeCloseTo(ref.pixel[0], 5);
      expect(got.pixels[i][1]).toBeCloseTo(ref.pixel[1], 5);
      expect(got.visible[i]).toBe(ref.visible);
    });
  });
});

describe("unprojectPixelAtDepth", () => {
  const SIMPLE_CALIB: SensorCalibration = {
    extrinsic: [
      1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
    ] as unknown as SensorCalibration["extrinsic"],
    intrinsic: [1000, 0, 500, 0, 1000, 500, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
  };

  function expectPointClose(
    actual: [number, number, number] | null,
    expected: readonly [number, number, number],
  ) {
    expect(actual).not.toBeNull();
    expected.forEach((value, index) => expect(actual![index]).toBeCloseTo(value, 8));
  }

  it("identity 外参按给定深度恢复世界点", () => {
    expectPointClose(unprojectPixelAtDepth([700, 900], 5, SIMPLE_CALIB), [1, 2, 5]);
  });

  it("真实外参与非平凡 rect 均可完成投影 round-trip", () => {
    const rect = [
      0.999, -0.01, 0.005, 0.02, 0.01, 0.9998, -0.002, -0.01, -0.005, 0.002, 1.0, 0.03, 0, 0, 0, 1,
    ] as unknown as SensorCalibration["rect"];
    const calibration: SensorCalibration = { ...FRONT_CALIB, rect };
    const point = [36.66, -34.4, 0.8] as const;
    const projected = projectPoints([point], calibration);

    expect(projected.visible[0]).toBe(true);
    expectPointClose(
      unprojectPixelAtDepth(projected.pixels[0], projected.depths[0], calibration),
      point,
    );
  });

  it("拒绝非正深度、非有限像素和不可逆矩阵", () => {
    expect(unprojectPixelAtDepth([700, 900], 0, SIMPLE_CALIB)).toBeNull();
    expect(unprojectPixelAtDepth([NaN, 900], 5, SIMPLE_CALIB)).toBeNull();

    const singularIntrinsic: SensorCalibration = {
      ...SIMPLE_CALIB,
      intrinsic: [0, 0, 0, 0, 0, 0, 0, 0, 0],
    };
    expect(unprojectPixelAtDepth([700, 900], 5, singularIntrinsic)).toBeNull();

    const singularExtrinsic: SensorCalibration = {
      ...SIMPLE_CALIB,
      extrinsic: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    };
    expect(unprojectPixelAtDepth([700, 900], 5, singularExtrinsic)).toBeNull();

    const singularRect: SensorCalibration = {
      ...SIMPLE_CALIB,
      rect: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    };
    expect(unprojectPixelAtDepth([700, 900], 5, singularRect)).toBeNull();
  });
});

describe("BOX_EDGES", () => {
  it("12 条边: 底面环 + 顶面环 + 4 竖棱, 索引均在 0..7", () => {
    expect(BOX_EDGES).toHaveLength(12);
    for (const [a, b] of BOX_EDGES) {
      expect(a).toBeGreaterThanOrEqual(0);
      expect(a).toBeLessThanOrEqual(7);
      expect(b).toBeGreaterThanOrEqual(0);
      expect(b).toBeLessThanOrEqual(7);
    }
    expect(BOX_EDGES.slice(8)).toEqual([
      [0, 4],
      [1, 5],
      [2, 6],
      [3, 7],
    ]);
  });
});
