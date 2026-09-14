import { execFile, execFileSync, spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { Page } from "@playwright/test";
import {
  ensureMacCaptureHelper,
  macCaptureReady,
} from "../../../scripts/mac-marketing-capture.mjs";
import { marketingCaptureDriver } from "../recording-plan.mjs";
import {
  MARKETING_CAPTURE_FPS,
  MARKETING_CAPTURE_SIZE,
  type MarketingCaptureCadence,
} from "./marketing-recorder.ts";

export const MARKETING_LOGICAL_VIEWPORT = { width: 1440, height: 810 } as const;
export const MARKETING_SOURCE_SIZE = { width: 2592, height: 1458 } as const;
export const MARKETING_DEVICE_SCALE_FACTOR = 1.8;
export const MARKETING_MAC_DEVICE_SCALE_FACTOR = 2;
const MAC_SOURCE_SIZE = { width: 2880, height: 1620 };

export interface BrowserMetrics {
  innerWidth: number;
  innerHeight: number;
  outerWidth: number;
  outerHeight: number;
  deviceScaleFactor: number;
  screenX: number;
  screenY: number;
}

export interface CaptureGeometry {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function correctedOuterSize(
  metrics: BrowserMetrics,
  previousRequest?: { width: number; height: number },
): {
  width: number;
  height: number;
} {
  const base = previousRequest ?? {
    width: metrics.outerWidth,
    height: metrics.outerHeight,
  };
  return {
    width: Math.max(
      1,
      Math.round(base.width + (MARKETING_LOGICAL_VIEWPORT.width - metrics.innerWidth)),
    ),
    height: Math.max(
      1,
      Math.round(base.height + (MARKETING_LOGICAL_VIEWPORT.height - metrics.innerHeight)),
    ),
  };
}

export interface ExternalMarketingCapture {
  extension: "mkv";
  logicalViewport: { width: number; height: number };
  deviceScaleFactor: number;
  driver: ExternalCaptureDriver;
  startedAtEpochMs: number;
  sourcePhysicalSize: { width: number; height: number };
  cadence: MarketingCaptureCadence;
  saveAs(target: string): Promise<void>;
  cleanup(): void;
}

export type ExternalCaptureDriver = "x11grab" | "gpu-screen-recorder" | "screencapturekit";

interface MacWindow {
  window_id: number;
  pid: number;
  width: number;
  height: number;
  point_width: number;
  point_height: number;
}

interface RunningRecorder {
  process: ChildProcess;
  sourcePath: string;
  temporaryRoot: string;
  stderr: string;
  startedAtEpochMs: number;
  cadenceWindow: { startEpochMs: number; endEpochMs: number };
  driver: ExternalCaptureDriver;
  geometry: CaptureGeometry;
  cadence: MarketingCaptureCadence;
  completion: Promise<number>;
}

const runningRecorders = new WeakMap<Page, RunningRecorder>();

export function x11CaptureInput(display: string): string {
  return /\.\d+$/.test(display) ? display : `${display}.0`;
}

export function externalCaptureFfmpegArgs(
  input: string,
  windowId: string,
  output: string,
  geometry: CaptureGeometry,
): string[] {
  return [
    "-hide_banner",
    "-loglevel",
    "warning",
    "-y",
    "-thread_queue_size",
    "128",
    "-f",
    "x11grab",
    "-draw_mouse",
    "0",
    "-window_id",
    windowId,
    "-framerate",
    String(MARKETING_CAPTURE_FPS),
    "-i",
    input,
    "-an",
    "-vf",
    `crop=${geometry.width}:${geometry.height}:${geometry.x}:${geometry.y},` +
      `fps=${MARKETING_CAPTURE_FPS}`,
    "-c:v",
    "h264_nvenc",
    "-preset",
    "p1",
    "-tune",
    "ll",
    "-rc",
    "constqp",
    "-qp",
    "12",
    "-bf",
    "0",
    "-pix_fmt",
    "bgr0",
    "-g",
    String(MARKETING_CAPTURE_FPS * 2),
    "-color_range",
    "tv",
    "-colorspace",
    "bt709",
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
    "-f",
    "matroska",
    output,
  ];
}

export function gpuScreenRecorderArgs(windowId: string, output: string): string[] {
  return [
    "-w",
    windowId,
    "-c",
    "mkv",
    "-k",
    "h264",
    "-f",
    String(MARKETING_CAPTURE_FPS),
    "-fm",
    "cfr",
    "-bm",
    "qp",
    "-q",
    "ultra",
    "-cr",
    "limited",
    "-tune",
    "performance",
    "-encoder",
    "gpu",
    "-fallback-cpu-encoding",
    "no",
    "-cursor",
    "no",
    "-keyint",
    "2",
    "-exclude-metadata",
    "yes",
    "-write-first-frame-ts",
    "yes",
    "-o",
    output,
  ];
}

export function gpuScreenRecorderFirstFrameEpochMs(timestamp: string): number {
  const values = timestamp
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.split(/\s+/))
    .find(
      (fields) =>
        fields.length >= 2 && /^\d+$/.test(fields[0] ?? "") && /^\d+$/.test(fields[1] ?? ""),
    );
  const realtimeMicroseconds = values?.[1] ? Number(values[1]) : Number.NaN;
  if (!Number.isFinite(realtimeMicroseconds)) {
    throw new Error("[marketing] GPU 录制器未返回可解析的首帧时间戳");
  }
  return realtimeMicroseconds / 1_000;
}

export function captureGeometry(
  metrics: BrowserMetrics,
  measured: CaptureGeometry,
  driver: ExternalCaptureDriver = "x11grab",
): CaptureGeometry {
  const dpr =
    driver === "screencapturekit"
      ? MARKETING_MAC_DEVICE_SCALE_FACTOR
      : MARKETING_DEVICE_SCALE_FACTOR;
  const size = driver === "screencapturekit" ? MAC_SOURCE_SIZE : MARKETING_SOURCE_SIZE;
  if (
    metrics.innerWidth !== MARKETING_LOGICAL_VIEWPORT.width ||
    metrics.innerHeight !== MARKETING_LOGICAL_VIEWPORT.height
  ) {
    throw new Error(
      `[marketing] 逻辑 viewport 必须为 ${MARKETING_LOGICAL_VIEWPORT.width}×${MARKETING_LOGICAL_VIEWPORT.height}，` +
        `实际为 ${metrics.innerWidth}×${metrics.innerHeight}`,
    );
  }
  if (
    !Number.isFinite(metrics.deviceScaleFactor) ||
    Math.abs(metrics.deviceScaleFactor - dpr) > 0.001
  ) {
    throw new Error(
      `[marketing] 设备像素倍率必须为 ${dpr}，` + `实际为 ${metrics.deviceScaleFactor}`,
    );
  }
  if (
    measured.width !== size.width ||
    measured.height !== size.height ||
    !Number.isInteger(measured.x) ||
    !Number.isInteger(measured.y) ||
    measured.x < 0 ||
    measured.y < 0
  ) {
    throw new Error(
      `[marketing] 校准内容区必须为 ${size.width}×${size.height}，` +
        `实际为 ${measured.width}×${measured.height}`,
    );
  }
  return measured;
}

export function calibrationBoundsFromRgb(
  rgb: Buffer,
  frameWidth: number,
  frameHeight: number,
  target: readonly [number, number, number],
): CaptureGeometry {
  if (rgb.length !== frameWidth * frameHeight * 3) {
    throw new Error("[marketing] 内容区校准帧尺寸异常");
  }
  let minX = frameWidth;
  let minY = frameHeight;
  let maxX = -1;
  let maxY = -1;
  const tolerance = 6;
  for (let y = 0; y < frameHeight; y += 1) {
    for (let x = 0; x < frameWidth; x += 1) {
      const offset = (y * frameWidth + x) * 3;
      if (
        Math.abs(rgb[offset]! - target[0]) > tolerance ||
        Math.abs(rgb[offset + 1]! - target[1]) > tolerance ||
        Math.abs(rgb[offset + 2]! - target[2]) > tolerance
      ) {
        continue;
      }
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
  }
  if (maxX < minX || maxY < minY) {
    throw new Error("[marketing] 未在采集窗口中找到内容区校准色");
  }
  return {
    x: minX,
    y: minY,
    width: maxX - minX + 1,
    height: maxY - minY + 1,
  };
}

export function validateCaptureCadence(sample: {
  frameCount: number;
  uniqueFrameCount: number;
  durationMs: number;
}): void {
  const effectiveUniqueFps = sample.uniqueFrameCount / (sample.durationMs / 1000);
  const uniqueFrameRatio = sample.uniqueFrameCount / sample.frameCount;
  if (
    !Number.isFinite(sample.durationMs) ||
    sample.durationMs <= 0 ||
    !Number.isInteger(sample.frameCount) ||
    !Number.isInteger(sample.uniqueFrameCount) ||
    sample.uniqueFrameCount > sample.frameCount ||
    sample.frameCount < 58 ||
    effectiveUniqueFps < 55 ||
    uniqueFrameRatio < 0.9
  ) {
    throw new Error(
      `[marketing] 60Hz 校准失败：${sample.durationMs}ms 采集 ${sample.frameCount} 帧，` +
        `独立画面 ${sample.uniqueFrameCount} 帧（有效 ${effectiveUniqueFps.toFixed(2)}fps，` +
        `独立帧占比 ${(uniqueFrameRatio * 100).toFixed(1)}%）`,
    );
  }
}

const CADENCE_COLOR_FORMULA = {
  red: { multiplier: 97, offset: 0 },
  green: { multiplier: 57, offset: 80 },
  blue: { multiplier: 193, offset: 160 },
} as const;

export function cadenceCalibrationRgb(frame: number): [number, number, number] {
  return [CADENCE_COLOR_FORMULA.red, CADENCE_COLOR_FORMULA.green, CADENCE_COLOR_FORMULA.blue].map(
    ({ multiplier, offset }) => (frame * multiplier + offset) % 256,
  ) as [number, number, number];
}

function installMarketingCursor(): void {
  const cursorId = "marketing-capture-cursor";
  const mount = () => {
    const existing = document.getElementById(cursorId);
    if (existing) return existing;
    const cursor = document.createElement("div");
    cursor.id = cursorId;
    cursor.setAttribute("aria-hidden", "true");
    Object.assign(cursor.style, {
      position: "fixed",
      left: "0",
      top: "0",
      width: "18px",
      height: "24px",
      background: "white",
      clipPath: "polygon(0 0, 0 82%, 24% 63%, 40% 100%, 55% 93%, 39% 58%, 72% 58%)",
      filter: "drop-shadow(0 0 1px black) drop-shadow(1px 2px 1px rgba(0,0,0,.85))",
      pointerEvents: "none",
      visibility: "hidden",
      zIndex: "2147483647",
    });
    document.documentElement.appendChild(cursor);
    return cursor;
  };
  window.addEventListener(
    "pointermove",
    (event) => {
      const cursor = mount();
      cursor.style.visibility = "visible";
      cursor.style.transform = `translate3d(${event.clientX}px, ${event.clientY}px, 0)`;
    },
    true,
  );
  if (document.documentElement) mount();
  else window.addEventListener("DOMContentLoaded", mount, { once: true });
}

async function pageMetrics(page: Page): Promise<BrowserMetrics> {
  return page.evaluate(() => ({
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    outerWidth: window.outerWidth,
    outerHeight: window.outerHeight,
    deviceScaleFactor: window.devicePixelRatio,
    screenX: window.screenX,
    screenY: window.screenY,
  }));
}

async function configureNativeCaptureWindow(page: Page): Promise<void> {
  const session = await page.context().newCDPSession(page);
  const { windowId } = await session.send("Browser.getWindowForTarget");
  let requestedOuter: { width: number; height: number } | undefined;
  // macOS 26 的窗口平铺会把固定起点的连续新窗口逐个吸进平铺槽位（CDP 视角
  // 尺寸正确、CG 原生边界却被改写）。每次重试换一个起点，打断吸附判定。
  const retryOrigins = [
    { left: 22, top: 55 },
    { left: 64, top: 78 },
    { left: 108, top: 102 },
    { left: 152, top: 126 },
    { left: 196, top: 150 },
  ];
  for (let attempt = 0; attempt < retryOrigins.length; attempt += 1) {
    const metrics = await pageMetrics(page);
    if (
      metrics.innerWidth === MARKETING_LOGICAL_VIEWPORT.width &&
      metrics.innerHeight === MARKETING_LOGICAL_VIEWPORT.height
    ) {
      // CDP 满足后仍需 CG 确认：平铺下 CDP 报请求值、CG 报槽位值，只有两者
      // 一致才是真实全尺寸窗口。
      const native = await session.send("Browser.getWindowBounds", { windowId });
      const nativeWidth = native.bounds.width ?? 0;
      const nativeHeight = native.bounds.height ?? 0;
      const snapped =
        Math.abs(nativeWidth - metrics.outerWidth) > 2 ||
        Math.abs(nativeHeight - metrics.outerHeight) > 2;
      if (!snapped) return;
    }
    requestedOuter = correctedOuterSize(metrics, requestedOuter);
    await session.send("Browser.setWindowBounds", {
      windowId,
      bounds: {
        ...retryOrigins[attempt],
        width: requestedOuter.width,
        height: requestedOuter.height,
        windowState: "normal",
      },
    });
    await page.waitForTimeout(300);
  }
  const actual = await pageMetrics(page);
  throw new Error(
    `[marketing] 无法把逻辑 viewport 校正为 ${MARKETING_LOGICAL_VIEWPORT.width}×${MARKETING_LOGICAL_VIEWPORT.height}，` +
      `实际 inner=${actual.innerWidth}×${actual.innerHeight}、outer=${actual.outerWidth}×${actual.outerHeight}、` +
      `dpr=${actual.deviceScaleFactor}`,
  );
}

async function captureWindowId(page: Page, display: string): Promise<string> {
  const title = `marketing-capture-${randomUUID()}`;
  await page.evaluate((value) => {
    document.title = value;
  }, title);
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const tree = execFileSync("xwininfo", ["-display", display, "-root", "-tree"], {
      encoding: "utf8",
      env: { ...process.env, DISPLAY: display },
    });
    const line = tree.split("\n").find((candidate) => candidate.includes(title));
    const windowId = line?.trim().match(/^(0x[0-9a-f]+)/i)?.[1];
    if (windowId) return windowId;
    await page.waitForTimeout(100);
  }
  throw new Error("[marketing] 无法定位 Chromium 的 X11 窗口");
}

function parsePpm(payload: Buffer): { width: number; height: number; rgb: Buffer } {
  const header = payload.subarray(0, 128).toString("ascii");
  const match = header.match(/^P6\s+(\d+)\s+(\d+)\s+255\s/);
  if (!match) throw new Error("[marketing] FFmpeg 未返回可解析的 PPM 校准帧");
  const width = Number(match[1]);
  const height = Number(match[2]);
  const headerBytes = Buffer.byteLength(match[0], "ascii");
  const rgb = payload.subarray(headerBytes);
  if (rgb.length !== width * height * 3) {
    throw new Error(`[marketing] PPM 校准帧数据长度异常：${rgb.length} != ${width * height * 3}`);
  }
  return { width, height, rgb };
}

async function measureBrowserContentGeometry(
  page: Page,
  display: string,
  windowId: string,
): Promise<CaptureGeometry> {
  const calibrationColor = [22, 199, 132] as const;
  await page.setContent(
    `<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;` +
      `background:rgb(${calibrationColor.join(",")})}</style>`,
  );
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  const result = execFileSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-f",
      "x11grab",
      "-draw_mouse",
      "0",
      "-window_id",
      windowId,
      "-i",
      x11CaptureInput(display),
      "-frames:v",
      "1",
      "-f",
      "image2pipe",
      "-vcodec",
      "ppm",
      "pipe:1",
    ],
    {
      encoding: "buffer",
      env: { ...process.env, DISPLAY: display },
      maxBuffer: 64 * 1024 * 1024,
    },
  );
  const frame = parsePpm(result);
  const measured = calibrationBoundsFromRgb(frame.rgb, frame.width, frame.height, calibrationColor);
  return captureGeometry(await pageMetrics(page), measured);
}

async function measureMacWindow(
  page: Page,
  helper: string,
  temporaryRoot: string,
): Promise<{ window: MacWindow; geometry: CaptureGeometry }> {
  const browser = page.context().browser();
  if (!browser) throw new Error("[marketing] Chromium 已退出");
  const session = await browser.newBrowserCDPSession();
  let pid: number;
  try {
    const result = await session.send("SystemInfo.getProcessInfo");
    pid = Number(result.processInfo.find((item) => item.type === "browser")?.id);
  } finally {
    await session.detach();
  }
  if (!Number.isInteger(pid) || pid <= 0) throw new Error("[marketing] 无法核对 Chromium 进程身份");
  const title = `marketing-capture-${randomUUID()}`;
  const color = [22, 199, 132] as const;
  await page.setContent(
    `<title>${title}</title><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:rgb(${color.join(",")})}</style>`,
  );
  await page.bringToFront();
  const metrics = await pageMetrics(page);
  const snapshot = path.join(temporaryRoot, "calibration.png");
  const { stdout } = await promisify(execFile)(
    helper,
    [
      "snapshot",
      String(pid),
      title,
      String(metrics.outerWidth),
      String(metrics.outerHeight),
      snapshot,
    ],
    { encoding: "utf8", timeout: 20_000 },
  );
  const window = JSON.parse(stdout) as MacWindow;
  if (
    window.pid !== pid ||
    !Number.isInteger(window.window_id) ||
    window.window_id <= 0 ||
    ![window.width, window.height, window.point_width, window.point_height].every(
      (value) => Number.isFinite(value) && value > 0 && value <= 8192,
    )
  ) {
    throw new Error("[marketing] 原生窗口身份或尺寸无效");
  }
  const frame = parsePpm(
    execFileSync(
      "ffmpeg",
      [
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        snapshot,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "ppm",
        "pipe:1",
      ],
      { maxBuffer: 64 * 1024 * 1024 },
    ),
  );
  if (frame.width !== window.width || frame.height !== window.height)
    throw new Error("[marketing] 原生校准帧尺寸不匹配");
  const geometry = captureGeometry(
    metrics,
    calibrationBoundsFromRgb(frame.rgb, frame.width, frame.height, color),
    "screencapturekit",
  );
  return { window, geometry };
}

export function cadenceCaptureFilter(
  driver: ExternalCaptureDriver,
  geometry: CaptureGeometry,
): string {
  return (
    (driver === "screencapturekit"
      ? `crop=${geometry.width}:${geometry.height}:${geometry.x}:${geometry.y},fps=${MARKETING_CAPTURE_FPS},`
      : "") + "scale=320:180:flags=area"
  );
}

async function recordCadenceCalibration(
  page: Page,
): Promise<{ startEpochMs: number; endEpochMs: number }> {
  return page.evaluate(
    (colorFormula) =>
      new Promise<{ startEpochMs: number; endEpochMs: number }>((resolve) => {
        document.documentElement.style.background = "#000";
        document.body.style.margin = "0";
        document.body.style.width = "100vw";
        document.body.style.height = "100vh";
        document.body.style.background = "#101418";
        document.body.replaceChildren();
        const cadenceLayer = document.createElement("div");
        Object.assign(cadenceLayer.style, {
          position: "fixed",
          left: "-256px",
          top: "-256px",
          width: "calc(100vw + 512px)",
          height: "calc(100vh + 512px)",
          background:
            "repeating-linear-gradient(135deg,#071a2b 0 24px,#e7f6ff 24px 48px," +
            "#4ade80 48px 72px,#7c3aed 72px 96px)",
          willChange: "transform",
        });
        const marker = document.createElement("div");
        Object.assign(marker.style, {
          position: "fixed",
          left: "0",
          top: "0",
          width: "96px",
          height: "96px",
          willChange: "transform, background-color",
        });
        document.body.append(cadenceLayer, marker);
        let frame = 0;
        const paint = (now: number, startedAt: number, startEpochMs: number) => {
          frame += 1;
          const red = (frame * colorFormula.red.multiplier + colorFormula.red.offset) % 256;
          const green = (frame * colorFormula.green.multiplier + colorFormula.green.offset) % 256;
          const blue = (frame * colorFormula.blue.multiplier + colorFormula.blue.offset) % 256;
          // 大面积条纹只改变 GPU 合成层位置，不触发 2.6K 整页重绘；小色块编码
          // rAF 序号，便于识别合成帧是否被实际重复。
          marker.style.background = `rgb(${255 - red},${255 - green},${255 - blue})`;
          marker.style.transform =
            `translate3d(${(frame * 19) % Math.max(1, innerWidth - 96)}px,` +
            `${(frame * 11) % Math.max(1, innerHeight - 96)}px,0)`;
          if (now - startedAt < 1_100) {
            requestAnimationFrame((next) => paint(next, startedAt, startEpochMs));
          } else {
            requestAnimationFrame((end) =>
              resolve({
                startEpochMs,
                endEpochMs: performance.timeOrigin + end,
              }),
            );
          }
        };
        requestAnimationFrame((startedAt) => {
          const startEpochMs = performance.timeOrigin + startedAt;
          cadenceLayer.animate(
            [{ transform: "translate3d(0,0,0)" }, { transform: "translate3d(503px,251px,0)" }],
            { duration: 1_100, easing: "linear", fill: "forwards" },
          );
          paint(startedAt, startedAt, startEpochMs);
        });
      }),
    CADENCE_COLOR_FORMULA,
  );
}

function captureCadenceSample(recorder: RunningRecorder): {
  frameCount: number;
  uniqueFrameCount: number;
  durationMs: number;
} {
  const startSeconds = Math.max(
    0,
    (recorder.cadenceWindow.startEpochMs - recorder.startedAtEpochMs) / 1000,
  );
  const durationSeconds =
    (recorder.cadenceWindow.endEpochMs - recorder.cadenceWindow.startEpochMs) / 1000;
  const result = execFileSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      recorder.sourcePath,
      "-ss",
      startSeconds.toFixed(3),
      "-t",
      durationSeconds.toFixed(3),
      "-vf",
      cadenceCaptureFilter(recorder.driver, recorder.geometry),
      "-f",
      "framemd5",
      "pipe:1",
    ],
    { encoding: "utf8", maxBuffer: 4 * 1024 * 1024 },
  );
  const hashes = result
    .split("\n")
    .filter((line) => /^\d/.test(line))
    .map((line) => line.split(",").at(-1)?.trim())
    .filter((hash): hash is string => Boolean(hash));
  return {
    frameCount: hashes.length,
    uniqueFrameCount: new Set(hashes).size,
    durationMs: recorder.cadenceWindow.endEpochMs - recorder.cadenceWindow.startEpochMs,
  };
}

async function validateGpuAndRefresh(page: Page): Promise<void> {
  const browser = page.context().browser();
  if (!browser) throw new Error("[marketing] 无法读取 Chromium 实例");
  const session = await browser.newBrowserCDPSession();
  const { gpu } = await session.send("SystemInfo.getInfo");
  const renderer = String(gpu.auxAttributes.glRenderer ?? "");
  const gpuCompositing = gpu.featureStatus.gpu_compositing;
  if (gpuCompositing !== "enabled" || /swiftshader|llvmpipe|software/i.test(renderer)) {
    throw new Error(
      `[marketing] 4K60 必须使用硬件合成，实际 renderer=${renderer || "unknown"}，` +
        `gpu_compositing=${gpuCompositing}`,
    );
  }

  const frameDeltas = await page.evaluate(
    () =>
      new Promise<number[]>((resolve) => {
        const deltas: number[] = [];
        let previous: number | null = null;
        const sample = (timestamp: number) => {
          if (previous !== null) deltas.push(timestamp - previous);
          previous = timestamp;
          if (deltas.length >= 120) resolve(deltas);
          else requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      }),
  );
  const sorted = [...frameDeltas].sort((left, right) => left - right);
  const p95 = sorted[Math.floor(sorted.length * 0.95)];
  if (!p95 || p95 > 20) {
    throw new Error(`[marketing] 浏览器未稳定运行在 60Hz：rAF p95=${p95?.toFixed(2) ?? "?"}ms`);
  }
}

async function waitForRecorderStartup(recorder: RunningRecorder): Promise<void> {
  const result = await Promise.race([
    recorder.completion.then((code) => ({ code })),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), 600)),
  ]);
  if (result !== null)
    throw new Error(`[marketing] 录制器启动失败（exit ${result.code}）：${recorder.stderr.trim()}`);
}

export function waitForMacReady(child: ChildProcess, window: MacWindow): Promise<number> {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const finish = (error?: Error, timestamp?: number) => {
      clearTimeout(timer);
      child.stdout?.off("data", onData);
      child.off("error", onError);
      child.off("close", onClose);
      if (error) reject(error);
      else resolve(timestamp!);
    };
    const onError = (error: Error) => finish(error);
    const onClose = () => finish(new Error("[marketing] 原生录制器在首帧就绪前退出"));
    const onData = (chunk: Buffer) => {
      buffer += chunk.toString();
      if (buffer.length > 32_768) {
        finish(new Error("[marketing] 原生录制器响应过长"));
        return;
      }
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        try {
          const message = JSON.parse(line);
          if (message.event === "ready") {
            finish(undefined, macCaptureReady(message, window));
            return;
          }
        } catch (error) {
          finish(error instanceof Error ? error : new Error(String(error)));
          return;
        }
      }
    };
    const timer = setTimeout(
      () => finish(new Error("[marketing] 原生窗口未在 20 秒内提供有效首帧")),
      20_000,
    );
    child.stdout?.on("data", onData);
    child.once("error", onError);
    child.once("close", onClose);
  });
}

export async function startExternalMarketingRecording(page: Page): Promise<void> {
  const requested = process.env.MARKETING_CAPTURE_DRIVER;
  if (!requested)
    throw new Error("[marketing] 必须通过 screenshots:record --profile marketing 启动外部录制器");
  const driver = marketingCaptureDriver(
    process.platform,
    requested,
    (process.env.SCREENSHOT_RECORDING_FLOWS ?? "").split(",").filter(Boolean),
  ) as ExternalCaptureDriver;
  if (runningRecorders.has(page)) throw new Error("[marketing] 当前页面已经开始录制");
  await page.addInitScript(installMarketingCursor);
  await configureNativeCaptureWindow(page);
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-4k60-"));
  let recorder: RunningRecorder | undefined;
  try {
    let geometry: CaptureGeometry;
    let command: string;
    let args: string[];
    let window: MacWindow | undefined;
    const sourcePath = path.join(
      temporaryRoot,
      driver === "screencapturekit" ? "capture.mp4" : "capture.mkv",
    );
    if (driver === "screencapturekit") {
      const helper = ensureMacCaptureHelper(
        fileURLToPath(new URL("../../../../../", import.meta.url)),
      );
      ({ window, geometry } = await measureMacWindow(page, helper, temporaryRoot));
      command = helper;
      args = [
        "record",
        String(window.pid),
        String(window.window_id),
        String(window.width),
        String(window.height),
        String(Math.round(window.point_width)),
        String(Math.round(window.point_height)),
        sourcePath,
      ];
    } else {
      const display = process.env.MARKETING_CAPTURE_DISPLAY ?? process.env.DISPLAY;
      if (!display) throw new Error("[marketing] 缺少 MARKETING_CAPTURE_DISPLAY");
      const windowId = await captureWindowId(page, display);
      geometry = await measureBrowserContentGeometry(page, display, windowId);
      command =
        driver === "gpu-screen-recorder"
          ? (process.env.MARKETING_GPU_SCREEN_RECORDER ?? "gpu-screen-recorder")
          : "ffmpeg";
      args =
        driver === "gpu-screen-recorder"
          ? gpuScreenRecorderArgs(windowId, sourcePath)
          : externalCaptureFfmpegArgs(x11CaptureInput(display), windowId, sourcePath, geometry);
    }
    await validateGpuAndRefresh(page);
    const child = spawn(command, args, {
      env: {
        ...process.env,
        ...(driver === "screencapturekit"
          ? {}
          : { DISPLAY: process.env.MARKETING_CAPTURE_DISPLAY ?? process.env.DISPLAY }),
      },
      stdio: ["pipe", driver === "screencapturekit" ? "pipe" : "ignore", "pipe"],
    });
    const startedAtEpochMs = Date.now();
    const completion = new Promise<number>((resolve) => {
      child.once("error", (error) => {
        if (recorder) recorder.stderr += error.message;
        resolve(1);
      });
      child.once("close", (code) => resolve(code ?? 1));
    });
    recorder = {
      process: child,
      sourcePath,
      temporaryRoot,
      stderr: "",
      startedAtEpochMs,
      cadenceWindow: { startEpochMs: startedAtEpochMs, endEpochMs: startedAtEpochMs },
      driver,
      geometry,
      completion,
      cadence: {
        sample_duration_ms: 0,
        captured_frames: 0,
        unique_frames: 0,
        effective_unique_fps: 0,
        unique_frame_ratio: 0,
      },
    };
    child.stderr?.on("data", (chunk) => {
      recorder!.stderr = `${recorder!.stderr}${String(chunk)}`.slice(-8_000);
    });
    // A process that exits while stopping is reported by completion, not an unhandled EPIPE.
    child.stdin?.on("error", () => {});
    runningRecorders.set(page, recorder);
    if (window) {
      recorder.startedAtEpochMs = await waitForMacReady(child, window);
      await page.bringToFront();
      await page.waitForTimeout(200); // Drain preroll buffers before the shared cadence calibration.
    } else await waitForRecorderStartup(recorder);
    recorder.cadenceWindow = await recordCadenceCalibration(page);
    await page.goto("about:blank");
  } catch (error) {
    runningRecorders.delete(page);
    if (recorder) await stopProcess(recorder).catch(() => {}); // Preserve the startup failure after terminating its child.
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
    const detail = recorder?.stderr.trim();
    throw new Error(
      `${error instanceof Error ? error.message : error}${detail ? `\n${detail}` : ""}`,
    );
  }
}

export async function stopProcess(recorder: RunningRecorder): Promise<void> {
  if (recorder.process.exitCode === null && recorder.process.signalCode === null) {
    if (recorder.driver === "gpu-screen-recorder") recorder.process.kill("SIGINT");
    else recorder.process.stdin?.write("q\n");
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const result = await Promise.race([
    recorder.completion,
    new Promise<"timeout">((resolve) => {
      timer = setTimeout(() => resolve("timeout"), 10_000);
    }),
  ]);
  clearTimeout(timer);
  if (result === "timeout") {
    recorder.process.kill("SIGKILL");
    await recorder.completion;
    throw new Error("[marketing] 录制器未能在 10 秒内停止，已终止采集进程");
  }
  if (result !== 0)
    throw new Error(`[marketing] 录制失败（exit ${result}）：${recorder.stderr.trim()}`);
}

export function normalizationEncoderArgs(driver: ExternalCaptureDriver): string[] {
  return driver === "screencapturekit"
    ? ["-c:v", "libx264", "-preset", "medium", "-crf", "12"]
    : [
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p1",
        "-tune",
        "ll",
        "-rc",
        "constqp",
        "-qp",
        "12",
        "-bf",
        "0",
      ];
}

function normalizeExternalCapture(recorder: RunningRecorder): string {
  const probe = JSON.parse(
    execFileSync(
      "ffprobe",
      [
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        recorder.sourcePath,
      ],
      { encoding: "utf8" },
    ),
  ) as { streams?: Array<{ width?: number; height?: number }> };
  const width = probe.streams?.[0]?.width ?? 0;
  const height = probe.streams?.[0]?.height ?? 0;
  const { geometry } = recorder;
  const normalizedPath = path.join(recorder.temporaryRoot, "capture-4k.mkv");
  const capturesContentOnly = width === geometry.width && height === geometry.height;
  if (
    !capturesContentOnly &&
    (width < geometry.x + geometry.width || height < geometry.y + geometry.height)
  ) {
    throw new Error(
      `[marketing] GPU 录制窗口不足以裁出 4K 内容区：` +
        `source=${width}×${height}, crop=${geometry.width}×${geometry.height}+${geometry.x}+${geometry.y}`,
    );
  }
  execFileSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-y",
      "-i",
      recorder.sourcePath,
      "-an",
      "-vf",
      (capturesContentOnly
        ? ""
        : `crop=${geometry.width}:${geometry.height}:${geometry.x}:${geometry.y},`) +
        `scale=${MARKETING_CAPTURE_SIZE.width}:${MARKETING_CAPTURE_SIZE.height}:flags=lanczos,` +
        `fps=${MARKETING_CAPTURE_FPS}`,
      ...normalizationEncoderArgs(recorder.driver),
      "-pix_fmt",
      "yuv420p",
      "-g",
      String(MARKETING_CAPTURE_FPS * 2),
      "-f",
      "matroska",
      normalizedPath,
    ],
    { stdio: "pipe" },
  );
  return normalizedPath;
}

export async function stopExternalMarketingRecording(
  page: Page,
): Promise<ExternalMarketingCapture> {
  const recorder = runningRecorders.get(page);
  if (!recorder) throw new Error("[marketing] 当前页面没有正在运行的外部录制器");
  runningRecorders.delete(page);
  let sourcePath: string;
  try {
    await stopProcess(recorder);
    if (!fs.existsSync(recorder.sourcePath) || fs.statSync(recorder.sourcePath).size === 0) {
      throw new Error("[marketing] 录制器未生成 4K60 采集源");
    }
    if (recorder.driver === "gpu-screen-recorder") {
      const timestampPath = `${recorder.sourcePath}.ts`;
      if (!fs.existsSync(timestampPath)) {
        throw new Error("[marketing] GPU 录制器未写入首帧时间戳");
      }
      recorder.startedAtEpochMs = gpuScreenRecorderFirstFrameEpochMs(
        fs.readFileSync(timestampPath, "utf8"),
      );
    }
    const sample = captureCadenceSample(recorder);
    validateCaptureCadence(sample);
    const effectiveUniqueFps = sample.uniqueFrameCount / (sample.durationMs / 1000);
    const uniqueFrameRatio = sample.uniqueFrameCount / sample.frameCount;
    recorder.cadence = {
      sample_duration_ms: sample.durationMs,
      captured_frames: sample.frameCount,
      unique_frames: sample.uniqueFrameCount,
      effective_unique_fps: Number(effectiveUniqueFps.toFixed(2)),
      unique_frame_ratio: Number(uniqueFrameRatio.toFixed(4)),
    };
    sourcePath = normalizeExternalCapture(recorder);
  } catch (error) {
    fs.rmSync(recorder.temporaryRoot, { recursive: true, force: true });
    throw error;
  }
  return {
    extension: "mkv",
    logicalViewport: { ...MARKETING_LOGICAL_VIEWPORT },
    deviceScaleFactor:
      recorder.driver === "screencapturekit"
        ? MARKETING_MAC_DEVICE_SCALE_FACTOR
        : MARKETING_DEVICE_SCALE_FACTOR,
    driver: recorder.driver,
    startedAtEpochMs: recorder.startedAtEpochMs,
    sourcePhysicalSize: { width: recorder.geometry.width, height: recorder.geometry.height },
    cadence: recorder.cadence,
    saveAs: async (target) => fs.promises.copyFile(sourcePath, target),
    cleanup: () => fs.rmSync(recorder.temporaryRoot, { recursive: true, force: true }),
  };
}

export async function discardExternalMarketingRecording(page: Page): Promise<void> {
  const recorder = runningRecorders.get(page);
  if (!recorder) return;
  runningRecorders.delete(page);
  try {
    await stopProcess(recorder);
  } finally {
    fs.rmSync(recorder.temporaryRoot, { recursive: true, force: true });
  }
}
