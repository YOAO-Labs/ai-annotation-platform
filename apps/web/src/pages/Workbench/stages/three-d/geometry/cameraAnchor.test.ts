import { describe, it, expect } from "vitest";
import type { SensorCalibration } from "@/types";
import { cameraAnchor } from "./cameraAnchor";

function extWithForward(fx: number, fy: number, fz: number): SensorCalibration {
  const e = [1, 0, 0, 0, 0, 1, 0, 0, fx, fy, fz, 0, 0, 0, 0, 1];
  return {
    extrinsic: e as unknown as SensorCalibration["extrinsic"],
    intrinsic: [1, 0, 0, 0, 1, 0, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
  };
}

describe("cameraAnchor · 名字优先", () => {
  it("front/rear/left/right → 四主向", () => {
    expect(cameraAnchor(null, "front")).toBe("top");
    expect(cameraAnchor(null, "rear")).toBe("bottom");
    expect(cameraAnchor(null, "back")).toBe("bottom");
    expect(cameraAnchor(null, "left")).toBe("left");
    expect(cameraAnchor(null, "right")).toBe("right");
  });

  it("复合朝向先于简单命中(front_left ≠ front)", () => {
    expect(cameraAnchor(null, "front_left")).toBe("top-left");
    expect(cameraAnchor(null, "front-right")).toBe("top-right");
    expect(cameraAnchor(null, "rear_left")).toBe("bottom-left");
    expect(cameraAnchor(null, "back_right")).toBe("bottom-right");
  });

  it("大小写 / 前缀不敏感", () => {
    expect(cameraAnchor(null, "CAM_FRONT")).toBe("top");
    expect(cameraAnchor(null, "Camera/Left")).toBe("left");
  });
});

describe("cameraAnchor · 外参兜底(标准系 X=前/Y=左)", () => {
  it("无可识别名字时按光轴方位推", () => {
    expect(cameraAnchor(extWithForward(1, 0, 0), "cam_0")).toBe("top");
    expect(cameraAnchor(extWithForward(0, 1, 0), "cam_1")).toBe("left");
    expect(cameraAnchor(extWithForward(-1, 0, 0), "cam_2")).toBe("bottom");
    expect(cameraAnchor(extWithForward(0, -1, 0), "cam_3")).toBe("right");
  });

  it("光轴近垂直(水平退化)→ overflow", () => {
    expect(cameraAnchor(extWithForward(0, 0, -1), "cam_down")).toBe("overflow");
  });
});

describe("cameraAnchor · 兜底与回归", () => {
  it("无名字命中且无标定 → overflow", () => {
    expect(cameraAnchor(null, "sensor_x")).toBe("overflow");
  });

  it("回归:名字优先压过外参 —— 示例集 front 外参方位是 -Y(-91.9°),仍按名字落 top", () => {
    const sustechFront = extWithForward(-0.033, -0.999, 0.042);
    expect(cameraAnchor(sustechFront, "front")).toBe("top");
  });
});
