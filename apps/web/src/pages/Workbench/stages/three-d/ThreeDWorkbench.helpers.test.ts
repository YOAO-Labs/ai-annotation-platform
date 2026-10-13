// v0.16.x 第 2 批 · ThreeDWorkbench PSR 表单纯逻辑测试守护(伴随从 schedulePatch /
// handleFieldBlur 提炼,锁定字段校验与 form→geometry 转换行为)。
import { afterEach, describe, it, expect, vi } from "vitest";
import {
  isPsrFieldBad,
  enlargedCameraNavigationDirection,
  loadCameraSample,
  parsePsrForm,
  psrFormToGeometry,
} from "./ThreeDWorkbench.helpers";
import type { PsrField } from "./ThreeDWorkbench.helpers";
import type { SensorCalibration } from "@/types";
import { dispatchKey } from "../../state/hotkeys";

afterEach(() => {
  vi.unstubAllGlobals();
});

const FULL_FORM: Record<PsrField, string> = {
  cx: "1",
  cy: "2",
  cz: "3",
  l: "4",
  w: "5",
  h: "6",
  yaw: "90",
  pitch: "0",
  roll: "0",
};

describe("loadCameraSample", () => {
  it("取消过期上色任务时立即停止相机图加载", async () => {
    class PendingImage {
      crossOrigin = "";
      naturalWidth = 0;
      naturalHeight = 0;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      src = "";
    }
    vi.stubGlobal("Image", PendingImage);
    const controller = new AbortController();
    const calibration = {
      extrinsic: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
      intrinsic: [1, 0, 0, 0, 1, 0, 0, 0, 1],
    } as SensorCalibration;
    let settled = false;

    const sample = loadCameraSample(
      "https://assets.test/stale-colorize.jpg",
      calibration,
      controller.signal,
    ).then((result) => {
      settled = true;
      return result;
    });
    controller.abort();
    await Promise.resolve();

    expect(settled).toBe(true);
    await expect(sample).resolves.toBeNull();
  });
});

describe("isPsrFieldBad", () => {
  it("空字符串非法", () => expect(isPsrFieldBad("cx", "")).toBe(true));
  it("非数字非法", () => expect(isPsrFieldBad("cx", "abc")).toBe(true));
  it("尺寸字段 <= 0 非法", () => {
    expect(isPsrFieldBad("l", "0")).toBe(true);
    expect(isPsrFieldBad("w", "-1")).toBe(true);
  });
  it("中心/朝向字段允许 0 与负数", () => {
    expect(isPsrFieldBad("cx", "0")).toBe(false);
    expect(isPsrFieldBad("yaw", "-30")).toBe(false);
  });
  it("尺寸字段正数合法", () => expect(isPsrFieldBad("h", "1.5")).toBe(false));
});

describe("parsePsrForm", () => {
  it("全字段有效时 valid=true 并解析为数值", () => {
    const { values, valid } = parsePsrForm(FULL_FORM);
    expect(valid).toBe(true);
    expect(values.cx).toBe(1);
    expect(values.yaw).toBe(90);
  });
  it("尺寸为 0 时 valid=false", () => {
    expect(parsePsrForm({ ...FULL_FORM, l: "0" }).valid).toBe(false);
  });
  it("空字段时 valid=false", () => {
    expect(parsePsrForm({ ...FULL_FORM, cx: "" }).valid).toBe(false);
  });
  it("非有限数时 valid=false", () => {
    expect(parsePsrForm({ ...FULL_FORM, cz: "abc" }).valid).toBe(false);
  });
});

describe("psrFormToGeometry", () => {
  it("组装为 box_3d geometry,朝向°转弧度(rotation=[roll, pitch, yaw])", () => {
    const { values } = parsePsrForm(FULL_FORM);
    const deg = Math.PI / 180;
    expect(psrFormToGeometry(values, "raw")).toEqual({
      type: "box_3d",
      center: [1, 2, 3],
      size: [4, 5, 6],
      rotation: [0 * deg, 0 * deg, 90 * deg],
      convention_at_create: "raw",
    });
  });
});

describe("相机放大浮层的方向键归属", () => {
  it.each([
    ["ArrowLeft", -1, "prev"],
    ["ArrowRight", 1, "next"],
  ] as const)("%s 裸键切相机，Mod 组合只切题", (key, direction, taskDirection) => {
    expect(enlargedCameraNavigationDirection(new KeyboardEvent("keydown", { key }))).toBe(
      direction,
    );
    for (const modifier of ["ctrlKey", "metaKey"] as const) {
      const event = new KeyboardEvent("keydown", { key, [modifier]: true });
      expect(enlargedCameraNavigationDirection(event)).toBeNull();
      expect(
        dispatchKey(event, {
          stage: "threed",
          isInputFocused: false,
          hasSelection: true,
          pendingActive: false,
        }),
      ).toEqual({ type: "navigateTask", dir: taskDirection });
    }
    for (const modifier of ["altKey", "shiftKey"] as const) {
      expect(
        enlargedCameraNavigationDirection(new KeyboardEvent("keydown", { key, [modifier]: true })),
      ).toBeNull();
    }
  });
});
