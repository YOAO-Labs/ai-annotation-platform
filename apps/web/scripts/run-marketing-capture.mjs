import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { marketingCaptureDriver } from "../e2e/screenshots/recording-plan.mjs";
import { ensureMacCaptureHelper } from "./mac-marketing-capture.mjs";

// Chromium 的 2592×1458 内容区之外还需容纳左右边框与顶部工具栏。
const TARGET = { width: 2700, height: 1750 };
const resizeDisplay = process.argv.includes("--resize-display");
const preflightOnly = process.argv.includes("--preflight-only");
const playwrightArgs = process.argv
  .slice(2)
  .filter((arg) => !["--", "--resize-display", "--preflight-only"].includes(arg));
const display = process.env.MARKETING_CAPTURE_DISPLAY;
const captureDriver = marketingCaptureDriver(
  process.platform,
  process.env.MARKETING_CAPTURE_DRIVER,
  (process.env.SCREENSHOT_RECORDING_FLOWS ?? "").split(",").filter(Boolean),
);
const isMac = captureDriver === "screencapturekit";
if (
  isMac &&
  (process.env.SCREENSHOT_BACKEND_REQUIREMENTS !== "none" ||
    process.env.SCREENSHOT_RECORDING_PROFILE !== "marketing")
) {
  throw new Error(
    "Use screenshots:record -- --flow <id> --profile marketing for scoped Mac capture.",
  );
}
if (isMac && resizeDisplay) throw new Error("--resize-display applies only to Linux X11 capture.");
const gpuScreenRecorder = process.env.MARKETING_GPU_SCREEN_RECORDER ?? "gpu-screen-recorder";

if (!isMac && !display) {
  throw new Error(
    "[marketing] 缺少 MARKETING_CAPTURE_DISPLAY；请显式指定用于录制的本机 X11 display（例如 :0）",
  );
}

const sourceCommitResult = spawnSync("git", ["rev-parse", "HEAD"], {
  encoding: "utf8",
  env: process.env,
});
if (sourceCommitResult.error || sourceCommitResult.status !== 0) {
  const reason =
    sourceCommitResult.error?.message ??
    sourceCommitResult.stderr.trim() ??
    `exit ${sourceCommitResult.status}`;
  throw new Error(`[marketing] 无法读取当前 Git 提交：${reason}`);
}
const sourceCommit = sourceCommitResult.stdout.trim();
const inheritedRunId = process.env.MARKETING_RUN_ID;
const inheritedCreatedAt = process.env.MARKETING_RUN_CREATED_AT;
if (Boolean(inheritedRunId) !== Boolean(inheritedCreatedAt)) {
  throw new Error("[marketing] MARKETING_RUN_ID 与 MARKETING_RUN_CREATED_AT 必须同时提供");
}
const captureCreatedAt = inheritedCreatedAt ?? new Date().toISOString();
const captureRunId =
  inheritedRunId ??
  `${captureCreatedAt.replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z")}-${sourceCommit.slice(0, 12)}`;

const recorderEnv = {
  ...process.env,
  ...(!isMac ? { DISPLAY: display, MARKETING_CAPTURE_DISPLAY: display } : {}),
  MARKETING_CAPTURE_DRIVER: captureDriver,
  MARKETING_GPU_SCREEN_RECORDER: gpuScreenRecorder,
  MARKETING_RUN_ID: captureRunId,
  MARKETING_RUN_CREATED_AT: captureCreatedAt,
  ...(process.env.MARKETING_XAUTHORITY ? { XAUTHORITY: process.env.MARKETING_XAUTHORITY } : {}),
};

function checked(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    env: recorderEnv,
    ...options,
  });
  if (result.error || result.status !== 0) {
    const reason = result.error?.message ?? result.stderr.trim() ?? `exit ${result.status}`;
    throw new Error(`[marketing] ${command} ${args.join(" ")} 失败：${reason}`);
  }
  return result.stdout;
}

function currentScreenSize() {
  const output = checked("xrandr", ["--display", display, "--current"]);
  const match = output.match(/current\s+(\d+)\s+x\s+(\d+)/);
  if (!match) throw new Error("[marketing] 无法从 xrandr 读取当前显示尺寸");
  return { width: Number(match[1]), height: Number(match[2]) };
}

async function preflight() {
  const encoders = checked("ffmpeg", ["-hide_banner", "-encoders"]);
  checked("ffprobe", ["-version"]);
  if (!/\blibx264\b/.test(encoders))
    throw new Error("[marketing] ffmpeg requires libx264 for master encoding");
  if (isMac) {
    const helper = ensureMacCaptureHelper(fileURLToPath(new URL("../../../", import.meta.url)));
    checked(helper, ["check"]);
  } else {
    checked("xwininfo", ["-display", display, "-root"]);
    const devices = checked("ffmpeg", ["-hide_banner", "-devices"]);
    if (!/\bD\s+x11grab\b/.test(devices)) throw new Error("[marketing] 当前 ffmpeg 不支持 x11grab");
    if (!/\bV\S*\s+h264_nvenc\b/.test(encoders))
      throw new Error("[marketing] 当前 ffmpeg 不支持 h264_nvenc");
    if (captureDriver === "gpu-screen-recorder") {
      checked(gpuScreenRecorder, ["--version"]);
      const captureOptions = checked(gpuScreenRecorder, ["--list-capture-options"]);
      if (!captureOptions.split("\n").includes("window"))
        throw new Error("[marketing] GPU Screen Recorder 当前不支持 X11 窗口采集");
    }
  }

  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  let health;
  try {
    const response = await fetch(new URL(isMac ? "/health/db" : "/health", apiBase));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    health = await response.json();
  } catch (error) {
    throw new Error(`[marketing] 无法检查截图 API 健康状态：${error.message}`);
  }
  // Like portable manual capture, the explicitly non-ML Mac scope needs the
  // isolated API/database but does not require an otherwise unused ML worker.
  if (isMac) return;
  const workers = health?.checks?.celery?.workers ?? [];
  if (!workers.some((worker) => worker.name?.startsWith("screenshots@"))) {
    throw new Error(
      "[marketing] 截图 API 未发现 screenshots@ 专用 Celery worker；异步 AI 流程会落入错误数据库",
    );
  }
  const sharedWorkers = workers.filter((worker) => !worker.name?.startsWith("screenshots@"));
  if (sharedWorkers.length > 0) {
    throw new Error(
      `[marketing] 截图 API 与日常 worker 共用 Redis：${sharedWorkers.map((worker) => worker.name).join(", ")}`,
    );
  }
}

async function runPlaywright() {
  console.log(`[marketing] 本次归档批次：${captureRunId}`);
  const executable = path.resolve("node_modules/.bin/playwright");
  const child = spawn(
    executable,
    [
      "test",
      "--config=playwright.screenshots.config.ts",
      "--project=marketing-master",
      ...playwrightArgs,
    ],
    { env: recorderEnv, stdio: "inherit" },
  );
  const forwardInterrupt = () => child.kill("SIGINT");
  const forwardTerminate = () => child.kill("SIGTERM");
  process.once("SIGINT", forwardInterrupt);
  process.once("SIGTERM", forwardTerminate);
  const exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => resolve(code ?? (signal ? 1 : 0)));
  });
  process.removeListener("SIGINT", forwardInterrupt);
  process.removeListener("SIGTERM", forwardTerminate);
  if (exitCode !== 0) process.exitCode = exitCode;
}

await preflight();
const original = isMac ? TARGET : currentScreenSize();
const needsResize = original.width < TARGET.width || original.height < TARGET.height;
if (needsResize && !resizeDisplay) {
  throw new Error(
    `[marketing] 当前显示为 ${original.width}×${original.height}，4K60 营销录制至少需要 ` +
      `${TARGET.width}×${TARGET.height}；确认桌面会暂时调整后追加 --resize-display`,
  );
}

if (preflightOnly) {
  if (needsResize) {
    checked("xrandr", [
      "--display",
      display,
      "--dryrun",
      "--fb",
      `${TARGET.width}x${TARGET.height}`,
    ]);
  }
  console.log("[marketing] 基础采集预检通过；未调整显示或录制。");
  process.exit(0);
}

let resized = false;
try {
  if (needsResize) {
    checked("xrandr", [
      "--display",
      display,
      "--dryrun",
      "--fb",
      `${TARGET.width}x${TARGET.height}`,
    ]);
    checked("xrandr", ["--display", display, "--fb", `${TARGET.width}x${TARGET.height}`]);
    resized = true;
    console.log(
      `[marketing] 显示已临时从 ${original.width}×${original.height} 调整为 ` +
        `${TARGET.width}×${TARGET.height}`,
    );
  }
  await runPlaywright();
} finally {
  if (resized) {
    checked("xrandr", ["--display", display, "--fb", `${original.width}x${original.height}`]);
    console.log(`[marketing] 显示已恢复为 ${original.width}×${original.height}`);
  }
}
